"""Negatives and variants: the code a student is shown as safe, and how augmented views are made.

A **subject** is the code a view shows, with what is known about it: its role (``vulnerable``,
``no_finding``, ``insufficient_context`` or ``unknown``), its label and tier, its lineage and a reason when
it is excluded. They are built from reviewed records:

* the vulnerable side of a record (a positive when its label says so);
* the **fixed twin**, the fixed side of an accepted fix: no supported finding for the condition the fix
  removed. It is the first negative pool. A twin whose code is vulnerable in another record (a chained
  fix) is excluded, as is a twin identical to its vulnerable code or one whose parent was found
  not vulnerable or lacking context;
* **ordinary production code** (functions the fix left alone): unknown until reviewed, weight 0, chosen
  length-matched so length alone does not give the label away (``length_auc`` measures the shortcut);
* **protected uses**: units a person reviewed as no finding;
* **student false positives**: review candidates only, blocked until reviewed;
* **harmless variants** of an accepted subject: only from the registry of approved transforms, each with
  checked equivalence to the parent, its lineage and the parent's group, at most four per parent.
  Mutations that change the vulnerability condition are blocked: without a verifier their label is
  unknown; the accepted condition-changing view of a record is its fixed twin.
"""
from __future__ import annotations

import ast
import hashlib
import re
import textwrap
from dataclasses import dataclass, field

from app.backend.pipeline import funcsplit, leakage, review
from app.backend.pipeline.schemas import reviewed

MAX_VARIANTS_PER_PARENT = 4          # contract: data.max_accepted_augmented_views_per_parent
CONDITION_CHANGING = {"status": "blocked",
                      "reason": "a mutation that changes the vulnerability condition has an unknown label without a "
                                "verifier; the accepted condition-changing view of a record is its fixed twin"}
_ROLE_OF_LABEL = {"vulnerability_supported": "vulnerable", "no_supported_finding_in_scope": "no_finding",
                  "insufficient_context": "insufficient_context"}


@dataclass(frozen=True)
class Subject:
    subject_id: str
    kind: str
    role: str
    code: str
    language: str | None
    ecosystem: str | None
    path: str | None
    symbol: str | None
    label_state: str
    review_status: str
    weight: float
    parent_record_id: str | None = None
    pair_id: str | None = None
    issue_group_id: str | None = None
    label_basis: str | None = None
    evidence_grade: str | None = None
    condition_scope: str | None = None
    exclusion: str | None = None
    lineage: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {k: (dict(v) if isinstance(v, dict) else v) for k, v in self.__dict__.items()}


def _sid(*parts: str) -> str:
    return "sub_" + hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _grade(status: str) -> str | None:
    return {"auto_checked": "G0", "llm_adjudicated": "G1", "human_reviewed": "G1"}.get(status)


# -- vulnerable subjects and fixed twins ----------------------------------------------------------------------------------

TWIN_SCOPE = "the condition that the fix of the paired vulnerable code removes, in the code shown"


def subjects_from_records(records) -> list[Subject]:
    """The vulnerable subject and the fixed twin of every reviewed record."""
    records = sorted(records, key=lambda r: r["record_id"])
    vulnerable_keys: dict[str, set[str]] = {}
    for r in records:
        vulnerable_keys.setdefault(leakage.code_key(r.get("language"), r.get("vulnerable_code")), set()).add(r["record_id"])
    out: list[Subject] = []
    for r in records:
        rid, lang = r["record_id"], r.get("language")
        status, label = r.get("review_status") or "unreviewed", r.get("label_state") or "unreviewed"
        common = dict(language=lang, ecosystem=r.get("ecosystem"), path=r.get("file_path_after"), symbol=r.get("symbol"),
                      parent_record_id=rid, pair_id=rid, issue_group_id=r.get("issue_group_id"))
        out.append(Subject(subject_id=_sid(rid, "vulnerable"), kind="vulnerable", role=_ROLE_OF_LABEL.get(label, "unknown"),
                           code=r["vulnerable_code"], label_state=label, review_status=status,
                           weight=review.supervision_weight(status), label_basis=r.get("label_basis"),
                           evidence_grade=_grade(status), **common))
        exclusion = None
        if label == "insufficient_context":
            exclusion = "parent_insufficient_context"
        elif label == "no_supported_finding_in_scope":
            exclusion = "parent_not_vulnerable"
        else:
            fixed_key = leakage.code_key(lang, r.get("fixed_code"))
            if fixed_key == leakage.code_key(lang, r.get("vulnerable_code")):
                exclusion = "identical_to_vulnerable"
            elif vulnerable_keys.get(fixed_key, set()) - {rid}:
                exclusion = "chained_vulnerable_elsewhere"
        if r.get('admission_policy') in ('automatic-evidence/1', 'automatic-evidence/2', 'automatic-evidence/3') and label == 'vulnerability_supported':
            from app.backend.pipeline import security_quality
            if security_quality.admission_problems(r, fixed_twin=True):
                exclusion = 'missing_scoped_negative_evidence'
        granted = label == "vulnerability_supported" and reviewed.tier_at_least(status, "auto_checked")
        t_status = status if granted else "unreviewed"
        out.append(Subject(subject_id=_sid(rid, "fixed_twin"), kind="fixed_twin", role="no_finding", code=r["fixed_code"],
                           label_state="no_supported_finding_in_scope" if granted else "unreviewed", review_status=t_status,
                           weight=0.0 if exclusion else review.supervision_weight(t_status),
                           label_basis=r.get("label_basis") if granted else None,
                           evidence_grade=_grade(t_status), condition_scope=TWIN_SCOPE, exclusion=exclusion, **common))
    return out


def eligible(s: Subject, min_tier: str = "auto_checked") -> bool:
    """A subject supervises a model only when it is not excluded, has weight and reaches the tier."""
    return s.exclusion is None and s.weight > 0 and reviewed.tier_at_least(s.review_status, min_tier)


def pool(subjects, role: str, min_tier: str = "auto_checked") -> list[Subject]:
    return [s for s in subjects if s.role == role and eligible(s, min_tier)]


# -- ordinary production code, protected uses, student errors ----------------------------------------------------------------

def ordinary_subjects(items) -> list[Subject]:
    """Functions the fix left untouched: unknown until a person reviews them (weight 0)."""
    out = []
    for it in items:
        out.append(Subject(subject_id=_sid("ordinary", it.get("path") or "", it.get("symbol") or "", it["code"]),
                           kind="ordinary_production", role="unknown", code=it["code"], language=it.get("language"),
                           ecosystem=it.get("ecosystem"), path=it.get("path"), symbol=it.get("symbol"),
                           label_state="unreviewed", review_status="unreviewed", weight=0.0,
                           parent_record_id=it.get("source_record_id"), issue_group_id=it.get("issue_group_id")))
    return sorted(out, key=lambda s: s.subject_id)


def _length(s: Subject) -> int:
    return len(funcsplit.tokens(s.language, s.code))


def length_match(targets, candidates, *, tolerance: float = 0.25, seed: str = "") -> dict:
    """For each target the unused candidate of the same language whose token length is closest, within the
    tolerance (ties: the smaller id). Targets are served in a seeded order so none is favoured by position."""
    order = sorted(targets, key=lambda t: hashlib.sha1(f"{seed}|{t.subject_id}".encode()).hexdigest())
    lens = {c.subject_id: _length(c) for c in candidates}
    used: set[str] = set()
    matches, unmatched = {}, []
    for t in order:
        n = max(_length(t), 1)
        best = None
        for c in sorted(candidates, key=lambda c: c.subject_id):
            if c.subject_id in used or c.language != t.language:
                continue
            diff = abs(lens[c.subject_id] - n)
            if diff / n <= tolerance and (best is None or diff < best[0]):
                best = (diff, c.subject_id)
        if best:
            matches[t.subject_id] = best[1]
            used.add(best[1])
        else:
            unmatched.append(t.subject_id)
    return {"matches": dict(sorted(matches.items())), "unmatched": sorted(unmatched)}


def length_auc(pairs) -> float:
    """How well length alone separates the labels: the probability that a random positive is longer than a
    random negative (ties count half). 0.5 means length carries no label information."""
    pos = [n for n, y in pairs if y]
    neg = [n for n, y in pairs if not y]
    if not pos or not neg:
        return 0.5
    wins = sum((p > q) + 0.5 * (p == q) for p in pos for q in neg)
    return wins / (len(pos) * len(neg))


def protected_subjects(rows: dict, decisions: dict) -> list[Subject]:
    """Units a person reviewed as no supported finding (uses of the same APIs that are protected)."""
    out = []
    for rid, d in sorted(decisions.items()):
        if d["review_status"] != "human_reviewed" or d["label_state"] != "no_supported_finding_in_scope" or rid not in rows:
            continue
        r = rows[rid]
        out.append(Subject(subject_id=_sid(rid, "protected_use"), kind="protected_use", role="no_finding",
                           code=r["vulnerable_code"], language=r.get("language"), ecosystem=r.get("ecosystem"),
                           path=r.get("file_path_after"), symbol=r.get("symbol"), label_state=d["label_state"],
                           review_status="human_reviewed", weight=1.0, parent_record_id=rid,
                           issue_group_id=r.get("issue_group_id"), label_basis="human_review", evidence_grade="G1"))
    return out


def student_false_positives(predictions, known_vulnerable, language_of=lambda p: p.get("language")) -> list[Subject]:
    """Code a student flagged that is not a known vulnerable unit: candidates for a person to review, never
    negatives by themselves."""
    known_codes = list(known_vulnerable)
    keys_by_language: dict = {}
    out = []
    for p in predictions:
        lang = language_of(p)
        if not p.get("predicted_finding"):
            continue
        if lang not in keys_by_language:                  # keys are computed with the tokenizer of each language
            keys_by_language[lang] = {leakage.code_key(lang, c) for c in known_codes}
        if leakage.code_key(lang, p["code"]) in keys_by_language[lang]:
            continue
        out.append(Subject(subject_id=_sid("student_fp", p.get("path") or "", p["code"]), kind="student_false_positive",
                           role="unknown", code=p["code"], language=lang, ecosystem=p.get("ecosystem"), path=p.get("path"),
                           symbol=p.get("symbol"), label_state="unreviewed", review_status="unreviewed", weight=0.0,
                           issue_group_id=p.get("issue_group_id"), exclusion="blocked_until_reviewed"))
    return sorted(out, key=lambda s: s.subject_id)


# -- harmless variants ----------------------------------------------------------------------------------------------------------------

_FORBIDDEN_CALLS = {"eval", "exec", "locals", "vars", "globals", "__import__", "dir"}
_COMMENTS = ("TODO: tidy up", "see the docs for details", "kept for compatibility", "keep in sync with the caller")
_HASH_COMMENT = {"python", "ruby", "shell", "perl", "elixir", "r", "yaml"}
_SLASH_COMMENT = {"javascript", "typescript", "java", "kotlin", "scala", "go", "c", "cpp", "csharp", "rust", "swift", "php"}


def _pick(seed: str, n: int) -> int:
    return int(hashlib.sha1(seed.encode()).hexdigest(), 16) % max(n, 1)


def _reformat(code: str, lang: str | None, seed: str) -> str | None:
    lines = code.split("\n")
    if len(lines) < 3:
        return None
    k = 1 + _pick(seed, min(3, len(lines) - 1))
    return "\n".join(lines[:k] + [""] + lines[k:])


def _comment_insert(code: str, lang: str | None, seed: str) -> str | None:
    text = _COMMENTS[_pick(seed, len(_COMMENTS))]
    if lang in _HASH_COMMENT:
        body = [l for l in code.rstrip("\n").split("\n") if l.strip()]
        indent = re.match(r"\s*", body[-1]).group(0) if body else ""
        return code.rstrip("\n") + f"\n{indent}# {text}\n"
    if lang in _SLASH_COMMENT:
        return code.rstrip("\n") + f"\n// {text}\n"
    return None


class _Rename(ast.NodeTransformer):
    def __init__(self, mapping):
        self.mapping = mapping

    def visit_Name(self, node):
        return ast.copy_location(ast.Name(id=self.mapping.get(node.id, node.id), ctx=node.ctx), node)

    def visit_arg(self, node):
        self.generic_visit(node)
        node.arg = self.mapping.get(node.arg, node.arg)
        return node

    def visit_ExceptHandler(self, node):
        self.generic_visit(node)
        if node.name:
            node.name = self.mapping.get(node.name, node.name)
        return node


def _renamable(tree) -> list[str] | None:
    """The parameters and local names of a single, simple function in first-appearance order, or None when
    the function is not safe to rename (nested scopes, imports, global/nonlocal/del, scope inspection)."""
    if len(tree.body) != 1 or not isinstance(tree.body[0], (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    fn = tree.body[0]
    order: list[str] = []

    def note(name):
        if name not in order and name not in ("self", "cls"):
            order.append(name)
    a = fn.args
    for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs, *([a.vararg] if a.vararg else []), *([a.kwarg] if a.kwarg else [])]:
        note(arg.arg)
    for n in ast.walk(fn):
        if n is fn:
            continue
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef, ast.Import, ast.ImportFrom,
                          ast.Global, ast.Nonlocal, ast.Delete)):
            return None
        if isinstance(n, ast.Name) and n.id in _FORBIDDEN_CALLS:
            return None
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            note(n.id)
        if isinstance(n, ast.ExceptHandler) and n.name:
            note(n.name)
    return order


def _rename_locals(code: str, lang: str | None, seed: str) -> str | None:
    if lang != "python":
        return None
    try:
        tree = ast.parse(textwrap.dedent(code))
    except SyntaxError:
        return None
    names = _renamable(tree)
    if not names:
        return None
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} \
        | {n.arg for n in ast.walk(tree) if isinstance(n, ast.arg)}
    prefix = ("tmp", "var", "item")[_pick(seed, 3)]
    mapping, i = {}, 0
    for name in names:
        while f"{prefix}{i}" in used or f"{prefix}{i}" in mapping.values():
            i += 1
        mapping[name] = f"{prefix}{i}"
        i += 1
    new = ast.unparse(ast.fix_missing_locations(_Rename(mapping).visit(tree))) + "\n"
    return None if new.strip() == code.strip() else new


def alpha_canonical(code: str) -> str:
    """The code with its parameters and locals renamed by first appearance, as an AST dump: two functions that
    differ only in those names are equal. A function that cannot be renamed safely is compared as it is."""
    try:
        tree = ast.parse(textwrap.dedent(code))
    except SyntaxError:
        return "raw:" + "\n".join(l.rstrip() for l in code.strip().splitlines())
    names = _renamable(tree)
    if names:
        tree = _Rename({n: f"_a{i}" for i, n in enumerate(names)}).visit(tree)
    return ast.dump(tree, annotate_fields=True, include_attributes=False)


def _layout_equivalent(lang, a, b) -> bool:
    return funcsplit.is_noop(lang, a, b)


TRANSFORMS: dict[str, dict] = {
    "reformat": {"name": "reformat", "version": 1, "approved": True, "applies": lambda lang: True,
                 "apply": _reformat, "equivalent": _layout_equivalent, "evidence": "layout-insensitive token equality"},
    "comment_insert": {"name": "comment_insert", "version": 1, "approved": True,
                       "applies": lambda lang: lang in _HASH_COMMENT or lang in _SLASH_COMMENT,
                       "apply": _comment_insert, "equivalent": _layout_equivalent,
                       "evidence": "comment-insensitive token equality"},
    "rename_locals": {"name": "rename_locals", "version": 1, "approved": True, "applies": lambda lang: lang == "python",
                      "apply": _rename_locals, "equivalent": lambda lang, a, b: alpha_canonical(a) == alpha_canonical(b),
                      "evidence": "alpha-equivalent AST"},
}


def approved_transforms() -> list[str]:
    """Transforms whose output is equivalent by construction and that invariance training may use."""
    return sorted(n for n, t in TRANSFORMS.items() if t["approved"])


def make_variants(parent: Subject, *, seed: str = "", existing=(), cap: int = MAX_VARIANTS_PER_PARENT) -> list[Subject]:
    """Accepted harmless variants of ``parent``: approved transforms only, equivalence checked, the parent's
    label, tier, weight and group, lineage recorded, and never more than ``cap`` per parent in total
    (``existing`` are the variants the parent already has)."""
    if parent.exclusion or parent.weight <= 0 or parent.review_status == "unreviewed":
        return []
    have = [v for v in existing if v.lineage.get("parent_subject_id") == parent.subject_id]
    room = cap - len(have)
    if room <= 0:
        return []
    seen_code = {parent.code, *[v.code for v in have]}
    found = []
    for name in approved_transforms():
        t = TRANSFORMS[name]
        if not t["applies"](parent.language):
            continue
        code = t["apply"](parent.code, parent.language, f"{seed}|{parent.subject_id}|{name}")
        if not code or code in seen_code or not t["equivalent"](parent.language, parent.code, code):
            continue
        seen_code.add(code)
        found.append(Subject(
            subject_id=_sid(parent.subject_id, name, code), kind="variant_harmless", role=parent.role, code=code,
            language=parent.language, ecosystem=parent.ecosystem, path=parent.path, symbol=parent.symbol,
            label_state=parent.label_state, review_status=parent.review_status, weight=parent.weight,
            parent_record_id=parent.parent_record_id, pair_id=parent.pair_id, issue_group_id=parent.issue_group_id,
            label_basis=parent.label_basis, evidence_grade=parent.evidence_grade, condition_scope=parent.condition_scope,
            lineage={"parent_subject_id": parent.subject_id, "transform": name, "transform_version": t["version"],
                     "equivalence": t["evidence"], "seed": seed}))
    found.sort(key=lambda s: hashlib.sha1(f"{seed}|{s.subject_id}".encode()).hexdigest())
    return found[:room]
