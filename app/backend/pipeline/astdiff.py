"""GumTree-style differencing of two syntax trees (used by ``funcsplit`` to describe how a function changed).

The text diff of a function says which lines changed; this says what happened to the syntax: which nodes were inserted, deleted, updated (a literal, an operator, a name) or moved (a
statement reordered, or wrapped in a new guard). It is metadata beside a pair: the pair's code is never replaced by it and no decision rests on it.

The algorithm follows Falleri et al., "Fine-grained and accurate source code differencing" (ASE 2014), with the cheaper recovery of the 2024 follow-up (GumTree Simple, ICSE 2024):

1. **top-down**: the largest isomorphic subtrees (same types and leaf text, found by hash) are mapped, tallest first, down to ``MIN_HEIGHT``; a subtree that has several equal twins is
   mapped only after the unambiguous ones, the pair whose parents already share the most mapped descendants (Dice) first;
2. **bottom-up**: an unmapped inner node is mapped to the node of the same type that shares the most mapped descendants with it when their Dice coefficient is above ``MIN_DICE``;
   below a mapped pair the children that are still unmapped are mapped by *simple recovery* (isomorphic subtrees, subtrees of the same shape, the only child of a type) instead of
   the cubic optimal tree edit distance of the original;
3. **edit script**: mapped nodes with another leaf text are updates; mapped nodes under another parent, or out of order among their siblings (all but a longest increasing
   subsequence), are moves; unmapped nodes of the new tree are inserts and unmapped nodes of the old one deletes, counted once per subtree root.

Comments and punctuation are not nodes (``(``, ``;`` and the like are layout); operators are, so ``<`` becoming ``<=`` is one update. Hashes are blake2b, never Python's ``hash``, so a result
is the same in every process. A tree over ``MAX_NODES`` nodes is not diffed.
"""
from __future__ import annotations

import bisect
import hashlib
import heapq
from collections import defaultdict

ALGO = "gumtree-lite-1"
MIN_HEIGHT = 2           # subtrees of this height or less are left to the bottom-up phase
MIN_DICE = 0.5           # the shared share of mapped descendants two inner nodes need to be mapped
MIN_SIMILAR = 0.5        # recovery pairs two unmapped subtrees of the same shape or the same type only when this much of their leaf text is alike...
GATE_SIZE = 5            # ...unless one of them is smaller than this (a slot under a mapped parent whose content changed is an update, whatever it holds)
MAX_NODES = 4000         # a tree larger than this is not diffed (the cost is quadratic in the worst case)
MAX_EDITS = 24           # edits listed beside the counts
_LAYOUT = frozenset("(){}[];,.:'\"`$")


class Node:
    __slots__ = ("type", "label", "children", "parent", "height", "size", "hash", "shape", "row", "start", "end", "id", "post", "left")

    def __init__(self, type_: str, label: str, row: int, start: int, end: int):
        self.type, self.label, self.row, self.start, self.end = type_, label, row, start, end
        self.children: list[Node] = []
        self.parent: Node | None = None
        self.height = self.size = self.id = self.post = self.left = 0
        self.hash = self.shape = ""


def _is_token(ts) -> bool:
    """An anonymous leaf that is an operator, not layout or a keyword: it has no letters, digits or only delimiter characters."""
    text = ts.text.decode("utf-8", "replace") if ts.text is not None else ""
    return bool(text) and not any(c.isalnum() or c == "_" for c in text) and not all(c in _LAYOUT for c in text)


def build(ts_root, limit: int | None = None):
    """(root, preorder list of nodes) for a tree-sitter node, or None when it has more than ``limit`` (default ``MAX_NODES``) nodes."""
    limit = MAX_NODES if limit is None else limit
    root = Node(ts_root.type, "", ts_root.start_point[0] + 1, ts_root.start_byte, ts_root.end_byte)
    stack = [(ts_root, root)]
    count = 1
    while stack:
        ts, node = stack.pop()
        kids = ts.children
        if not kids:
            if ts.is_named:
                node.label = ts.text.decode("utf-8", "replace") if ts.text is not None else ""
            continue
        made = []
        for c in kids:
            if "comment" in c.type:
                continue
            if not c.is_named and (c.children or not _is_token(c)):
                continue
            child = Node(c.type if c.is_named else "operator", "", c.start_point[0] + 1, c.start_byte, c.end_byte)
            if not c.is_named:
                child.label = c.text.decode("utf-8", "replace")                  # every operator is one kind of node, so another operator is an update
            child.parent = node
            made.append((c, child))
            count += 1
            if count > limit:
                return None
        node.children = [child for _, child in made]
        stack.extend(reversed(made))
    nodes: list[Node] = []
    order = [root]
    while order:                                          # preorder ids
        n = order.pop()
        n.id = len(nodes)
        nodes.append(n)
        order.extend(reversed(n.children))
    post: list[Node] = []
    stack2: list[tuple[Node, bool]] = [(root, False)]
    while stack2:                                         # postorder, heights, sizes, hashes
        n, done = stack2.pop()
        if not done:
            stack2.append((n, True))
            stack2.extend((c, False) for c in reversed(n.children))
            continue
        n.post = len(post)
        post.append(n)
        n.height = 1 + max((c.height for c in n.children), default=0)
        n.size = 1 + sum(c.size for c in n.children)
        n.left = n.children[0].left if n.children else n.post
        h = hashlib.blake2b(digest_size=8)
        s = hashlib.blake2b(digest_size=8)
        h.update(f"{n.type}\x00{n.label}\x00".encode("utf-8"))
        s.update(f"{n.type}\x00".encode("utf-8"))
        for c in n.children:
            h.update(c.hash.encode())
            s.update(c.shape.encode())
        n.hash, n.shape = h.hexdigest(), s.hexdigest()
    return root, nodes


class _Maps:
    def __init__(self, n1: int, n2: int):
        self.a: list[Node | None] = [None] * n1            # old node id -> new node
        self.b: list[Node | None] = [None] * n2            # new node id -> old node

    def link(self, x: Node, y: Node) -> None:
        self.a[x.id], self.b[y.id] = y, x

    def link_subtrees(self, x: Node, y: Node) -> None:
        """Map two isomorphic subtrees node by node."""
        stack = [(x, y)]
        while stack:
            p, q = stack.pop()
            self.link(p, q)
            stack.extend(zip(p.children, q.children))


def _descendants(n: Node, by_post: list[Node]):
    return by_post[n.left:n.post]


def _dice(x: Node, y: Node, maps: _Maps, by_post1: list[Node]) -> float:
    """Twice the mapped descendants the two nodes share, over their descendants."""
    total = (x.size - 1) + (y.size - 1)
    if total <= 0:
        return 0.0
    common = 0
    for d in _descendants(x, by_post1):
        m = maps.a[d.id]
        if m is not None and y.left <= m.post < y.post:
            common += 1
    return 2.0 * common / total


def _top_down(src: Node, dst: Node, nodes1: list[Node], nodes2: list[Node], maps: _Maps, by_post1: list[Node]) -> None:
    cnt1: dict[str, int] = defaultdict(int)
    cnt2: dict[str, int] = defaultdict(int)
    for n in nodes1:
        cnt1[n.hash] += 1
    for n in nodes2:
        cnt2[n.hash] += 1
    l1 = [(-src.height, src.id)]
    l2 = [(-dst.height, dst.id)]
    peek = lambda l: -l[0][0] if l else 0                  # noqa: E731

    def pop_group(l, nodes):
        h = peek(l)
        out = []
        while l and -l[0][0] == h:
            out.append(nodes[heapq.heappop(l)[1]])
        return out

    def open_(n, l):
        for c in n.children:
            heapq.heappush(l, (-c.height, c.id))
    ambiguous: list[tuple[Node, Node]] = []
    while min(peek(l1), peek(l2)) > MIN_HEIGHT:
        if peek(l1) > peek(l2):
            for t in pop_group(l1, nodes1):
                open_(t, l1)
            continue
        if peek(l2) > peek(l1):
            for t in pop_group(l2, nodes2):
                open_(t, l2)
            continue
        h1, h2 = pop_group(l1, nodes1), pop_group(l2, nodes2)
        by_hash2: dict[str, list[Node]] = defaultdict(list)
        for t in h2:
            by_hash2[t.hash].append(t)
        taken1: set[int] = set()
        taken2: set[int] = set()
        for t1 in h1:
            if maps.a[t1.id] is not None:
                continue
            for t2 in by_hash2.get(t1.hash, ()):
                if t2.id in taken2 or maps.b[t2.id] is not None:
                    continue
                if cnt1[t1.hash] == 1 or cnt2[t2.hash] == 1:           # no equal twin on one side: the mapping is certain
                    maps.link_subtrees(t1, t2)
                    taken1.add(t1.id)
                    taken2.add(t2.id)
                    break
                ambiguous.append((t1, t2))
                taken1.add(t1.id)
                taken2.add(t2.id)
        for t in h1:
            if t.id not in taken1:
                open_(t, l1)
        for t in h2:
            if t.id not in taken2:
                open_(t, l2)
    ranked = sorted(ambiguous, key=lambda p: (-_dice(p[0].parent, p[1].parent, maps, by_post1) if p[0].parent is not None and p[1].parent is not None else 0.0,
                                              abs(p[0].row - p[1].row), p[0].id, p[1].id))
    for t1, t2 in ranked:
        if maps.a[t1.id] is None and maps.b[t2.id] is None:
            maps.link_subtrees(t1, t2)


def _bag(n: Node, cache: dict) -> dict:
    """The leaf texts under a node with their counts (cached)."""
    got = cache.get(n.id)
    if got is None:
        got = {}
        stack = [n]
        while stack:
            x = stack.pop()
            if x.children:
                stack.extend(x.children)
            elif x.label:
                got[x.label] = got.get(x.label, 0) + 1
        cache[n.id] = got
    return got


def _similar(x: Node, y: Node, cx: dict, cy: dict) -> float:
    """How alike the leaf texts under two nodes are (Dice over the multisets of texts); two nodes without any are alike when they have the same type."""
    bx, by = _bag(x, cx), _bag(y, cy)
    total = sum(bx.values()) + sum(by.values())
    if not total:
        return 1.0 if x.type == y.type else 0.0
    return 2.0 * sum(min(c, by.get(k, 0)) for k, c in bx.items()) / total


def _recover(x: Node, y: Node, maps: _Maps, force: bool = False) -> None:
    """Map the children of a mapped pair that are still unmapped: isomorphic subtrees, then subtrees of the same shape, then the only child of a type; descend into the new pairs. The
    last two need ``MIN_SIMILAR`` of the leaf text in common (else a deleted statement would be taken for the edited form of an unrelated new one) unless one of the two is small or
    ``force`` (the children of a function that is known to correspond, its header and its body); among several candidates the most similar wins."""
    cx: dict = {}
    cy: dict = {}
    stack = [(x, y)]
    while stack:
        p, q = stack.pop()
        u1 = [c for c in p.children if maps.a[c.id] is None]
        u2 = [c for c in q.children if maps.b[c.id] is None]
        if not u1 or not u2:
            continue
        idx: dict[str, list[Node]] = defaultdict(list)
        for c in u2:
            idx[c.hash].append(c)
        for c in u1:
            for d in idx.get(c.hash, ()):
                if maps.b[d.id] is None:
                    maps.link_subtrees(c, d)
                    break
        pairs: list[tuple[Node, Node]] = []
        for kind in ("shape", "type"):
            rest1 = [c for c in u1 if maps.a[c.id] is None]
            rest2 = [c for c in u2 if maps.b[c.id] is None]
            if kind == "type":                                          # only a type that occurs once on each side
                t1 = defaultdict(list)
                t2 = defaultdict(list)
                for c in rest1:
                    t1[c.type].append(c)
                for c in rest2:
                    t2[c.type].append(c)
                rest1 = [c[0] for c in t1.values() if len(c) == 1 and len(t2.get(c[0].type, ())) == 1]
                rest2 = [t2[c.type][0] for c in rest1]
            cand = []
            for c in rest1:
                for d in rest2:
                    if (c.shape == d.shape) if kind == "shape" else (c.type == d.type):
                        sim = _similar(c, d, cx, cy)
                        if sim >= MIN_SIMILAR or force or min(c.size, d.size) < GATE_SIZE:
                            cand.append((-sim, abs(c.row - d.row), c.id, d.id, c, d))
            for _, _, _, _, c, d in sorted(cand, key=lambda t: t[:4]):
                if maps.a[c.id] is None and maps.b[d.id] is None:
                    maps.link(c, d)
                    pairs.append((c, d))
        stack.extend(pairs)
        force = False


def _bottom_up(src: Node, dst: Node, nodes1: list[Node], nodes2: list[Node], maps: _Maps, by_post1: list[Node], by_post2: list[Node]) -> None:
    for t1 in by_post1:
        if maps.a[t1.id] is not None or not t1.children:
            continue
        cands: dict[int, Node] = {}
        seen: set[int] = set()
        for d in _descendants(t1, by_post1):
            m = maps.a[d.id]
            if m is None:
                continue
            a = m.parent
            while a is not None and a.id not in seen:
                seen.add(a.id)
                if a.type == t1.type and maps.b[a.id] is None:
                    cands[a.id] = a
                a = a.parent
        best, best_dice = None, 0.0
        for c in cands.values():
            dc = _dice(t1, c, maps, by_post1)
            if dc > best_dice or (dc == best_dice and best is not None and abs(c.row - t1.row) < abs(best.row - t1.row)):
                best, best_dice = c, dc
        if best is not None and best_dice > MIN_DICE:
            maps.link(t1, best)
            _recover(t1, best, maps)


def _leftover_isomorphic(nodes1: list[Node], nodes2: list[Node], maps: _Maps) -> None:
    """Last refinement: subtrees that are still unmapped in both trees and equal (the same hash), mapped when each has exactly one twin. This catches the small statements the top-down phase
    leaves alone (a ``return x;`` is only two levels high) and that moved into a block that did not exist before."""
    free1: dict[str, list[Node]] = defaultdict(list)
    free2: dict[str, list[Node]] = defaultdict(list)
    for n in nodes1:
        if maps.a[n.id] is None and n.size >= 2:
            free1[n.hash].append(n)
    for n in nodes2:
        if maps.b[n.id] is None and n.size >= 2:
            free2[n.hash].append(n)
    todo = [(a[0], b[0]) for h, a in free1.items() if len(a) == 1 and len(b := free2.get(h, ())) == 1]
    for x, y in sorted(todo, key=lambda t: (-t[0].size, t[0].id)):
        if maps.a[x.id] is None and maps.b[y.id] is None and _unmapped_subtree(x, maps.a) and _unmapped_subtree(y, maps.b):
            maps.link_subtrees(x, y)


def _unmapped_subtree(n: Node, side: list) -> bool:
    stack = [n]
    while stack:
        x = stack.pop()
        if side[x.id] is not None:
            return False
        stack.extend(x.children)
    return True


def _lis(seq: list[int]) -> set[int]:
    """Indexes (into ``seq``) of one longest strictly increasing subsequence."""
    tails: list[int] = []
    tail_idx: list[int] = []
    prev = [-1] * len(seq)
    for i, v in enumerate(seq):
        k = bisect.bisect_left(tails, v)
        if k == len(tails):
            tails.append(v)
            tail_idx.append(i)
        else:
            tails[k] = v
            tail_idx[k] = i
        prev[i] = tail_idx[k - 1] if k else -1
    out, i = set(), tail_idx[-1] if tail_idx else -1
    while i != -1:
        out.add(i)
        i = prev[i]
    return out


def _brief(text: str, n: int = 40) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n - 1] + "~"


def diff(src_ts, dst_ts) -> dict:
    """The edit summary between two tree-sitter nodes (the root of a function before and after): ``{"algo", "nodes", "mapped", "counts", "edits", "truncated"}`` or ``{"skipped", "algo"}``."""
    a, b = build(src_ts), build(dst_ts)
    if a is None or b is None:
        return {"skipped": "too_large", "algo": ALGO}
    (src, nodes1), (dst, nodes2) = a, b
    by_post1 = sorted(nodes1, key=lambda n: n.post)
    by_post2 = sorted(nodes2, key=lambda n: n.post)
    maps = _Maps(len(nodes1), len(nodes2))
    if src.type == dst.type:                           # the two functions correspond: so do their headers and bodies, before anything else can claim them
        maps.link(src, dst)
        _recover(src, dst, maps, force=True)
    _top_down(src, dst, nodes1, nodes2, maps, by_post1)
    _bottom_up(src, dst, nodes1, nodes2, maps, by_post1, by_post2)
    _leftover_isomorphic(nodes1, nodes2, maps)
    edits: list[dict] = []
    counts = {"insert": 0, "delete": 0, "update": 0, "move": 0}

    def add(op: str, n: Node, **extra):
        counts[op] += 1
        edits.append({"op": op, "type": n.type, "line_before": extra.pop("line_before", None), "line_after": extra.pop("line_after", None), **extra})

    for n in nodes1:                                                        # updates and moves between parents
        m = maps.a[n.id]
        if m is None:
            continue
        if n.label != m.label:
            add("update", n, line_before=n.row, line_after=m.row, **{"from": _brief(n.label), "to": _brief(m.label)})
        if n.parent is not None and m.parent is not None and maps.a[n.parent.id] is not m.parent:
            add("move", n, line_before=n.row, line_after=m.row, size=n.size)
    for y in nodes2:                                                        # reordered among siblings: all but a longest increasing run of the matched children
        v = maps.b[y.id]
        if v is None or len(y.children) < 2:
            continue
        kids = [c for c in y.children if maps.b[c.id] is not None and maps.b[c.id].parent is v]
        if len(kids) < 2:
            continue
        pos = {c.id: i for i, c in enumerate(v.children)}
        order = [pos[maps.b[c.id].id] for c in kids]
        keep = _lis(order)
        for i, c in enumerate(kids):
            if i not in keep:
                o = maps.b[c.id]
                add("move", c, line_before=o.row, line_after=c.row, size=c.size)
    for y in nodes2:                                                        # inserted subtrees, counted by their root
        if maps.b[y.id] is None and (y.parent is None or maps.b[y.parent.id] is not None):
            add("insert", y, line_after=y.row, size=y.size)
    for x in nodes1:                                                        # deleted subtrees
        if maps.a[x.id] is None and (x.parent is None or maps.a[x.parent.id] is not None):
            add("delete", x, line_before=x.row, size=x.size)
    order_op = {"update": 0, "move": 1, "insert": 2, "delete": 3}
    edits.sort(key=lambda e: (e["line_after"] if e["line_after"] is not None else e["line_before"], order_op[e["op"]], e["type"], str(e.get("from", ""))))
    return {"algo": ALGO, "nodes": [len(nodes1), len(nodes2)], "mapped": sum(1 for m in maps.a if m is not None), "counts": counts, "edits": edits[:MAX_EDITS],
            "truncated": len(edits) > MAX_EDITS}
