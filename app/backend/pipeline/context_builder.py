"""The deterministic local context builder (``deterministic_local_snapshot_v1``).

What a model sees around a unit of code (a function, a hunk or a file) is gathered from the repository as
it was at the **parent commit**, by fixed rules that never look at the advisory, the fix, the diff or any
later snapshot (``Unit`` has no field to carry them). In priority order:

1. ``enclosing``      for a hunk, the function that contains it;
2. ``callee``         functions of the same file the unit refers to by name;
3. ``imports``        the file's import statements;
4. ``module_symbol``  the referenced definitions of the local modules the file imports, from blobs fetched
   *by path* at the parent commit (``wanted_paths`` says which; nothing is ever searched for);
5. ``caller``         functions of the same file that refer to the unit by name;
6. ``manifest``       the nearest dependency manifest (never a lock file).

Python is read with ``ast``; every language with a tree-sitter grammar gets function boundaries from
``funcsplit`` and name references from its tokens. A language without a parser, a file that does not
parse, or a missing file text leaves only the changed code (``changed_files_only``). Whatever could not be
gathered or did not fit a budget is listed in ``omissions``; the bundle is ``complete`` only when
nothing was omitted, ``partial`` otherwise. A bundle is addressed by the hash of its content, so one bundle
is shared by every model that is compared on the same record. The oracle variant (diff and advisory added)
exists only as a labelled diagnostic and is never allowed in the main evaluation.
"""
from __future__ import annotations

import ast
import functools
import hashlib
import json
import posixpath
import re
import textwrap
from dataclasses import dataclass, field

from app.backend.pipeline import ecosystems, funcsplit
from app.backend.pipeline import util

POLICY = "deterministic_local_snapshot_v1"
ORACLE_POLICY = "oracle_diagnostic_v1"
MAX_CALLEES, MAX_CALLERS = 12, 8
JS_LANGS = ("javascript", "typescript")
JS_EXTS = (".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs")
_LOCKISH = re.compile(r"lock|shrinkwrap", re.I)
_IMPORT_LINE = re.compile(r"^[ \t]*(?:import|using|use|require|require_relative|include|#include|from)\b[^\n]*$", re.M)
_JS_IMPORT = re.compile(r"""(?:import|export)\s+(?:[^'";]*?\s+from\s+)?['"]([^'"]+)['"]""")
_JS_REQUIRE = re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)""")


@dataclass(frozen=True)
class Budget:
    """Characters per kind and in total (token budgets are applied later, by the tokenizer)."""
    total: int = 12000
    imports: int = 1500
    callees: int = 4000
    callers: int = 3000
    modules: int = 3000
    config: int = 2000
    enclosing: int = 6000


@dataclass(frozen=True)
class Unit:
    """Everything the builder may use. There is deliberately no field for an advisory, a diff, a fix
    commit, a CWE or an explanation: they cannot reach the context."""
    path: str
    code: str                                   # the unit as it was at the parent commit
    file_text: str | None = None                # the whole file at the parent commit
    language: str | None = None
    ecosystem: str | None = None
    name: str | None = None                     # qualified function name; None for a hunk or a whole file
    blobs: dict = field(default_factory=dict)   # other files at the parent commit, by path
    missing_paths: frozenset = frozenset()      # paths known not to exist at the parent commit
    known_paths: frozenset | None = None        # every path of the parent commit, when listed: exact resolution
    resolution: dict = field(default_factory=dict)  # explicit JS/TS compiler path mappings from this snapshot


@dataclass(frozen=True)
class ContextItem:
    kind: str
    path: str
    name: str | None
    text: str
    reason: str
    target_sha256: str | None = None

    def as_dict(self) -> dict:
        return {"kind": self.kind, "path": self.path, "name": self.name, "text": self.text, "reason": self.reason,
                **({"target_sha256": self.target_sha256} if self.target_sha256 is not None else {})}


@dataclass
class Bundle:
    items: list[ContextItem]
    completeness: str
    omissions: list[str]
    policy: str = POLICY
    variant: str = "standard"
    main_evaluation_allowed: bool = True
    snapshot: dict | None = None

    def to_dict(self) -> dict:
        kinds: dict[str, int] = {}
        for i in self.items:
            kinds[i.kind] = kinds.get(i.kind, 0) + 1
        return {**({"snapshot": self.snapshot} if self.snapshot is not None else {}), "policy": self.policy, "variant": self.variant, "main_evaluation_allowed": self.main_evaluation_allowed,
                "completeness": self.completeness, "omissions": self.omissions,
                "stats": {"items": len(self.items), "chars": sum(len(i.text) for i in self.items), "kinds": kinds},
                "items": [i.as_dict() for i in self.items]}

    @property
    def bundle_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, d: dict) -> "Bundle":
        return cls([ContextItem(**i) for i in d["items"]], d["completeness"], list(d["omissions"]), d["policy"],
                   d["variant"], d["main_evaluation_allowed"], d.get("snapshot"))


# -- reading code --------------------------------------------------------------------------------------------------------

def _leaf(qual: str) -> str:
    return qual.split("#")[0].split(".")[-1]


@functools.lru_cache(maxsize=8192)
def _refs_cached(lang: str | None, text: str) -> frozenset:
    """Names a piece of code refers to: calls, attribute names and loaded names (Python), else identifiers."""
    if lang == "python":
        try:
            tree = ast.parse(textwrap.dedent(text))
            out: set[str] = set()
            for n in ast.walk(tree):
                if isinstance(n, ast.Name):
                    out.add(n.id)
                elif isinstance(n, ast.Attribute):
                    out.add(n.attr)
            return frozenset(out)
        except SyntaxError:
            pass
    return frozenset(t for t in funcsplit.tokens(lang, text) if t[:1].isalpha() or t[:1] == "_")


def _refs(lang: str | None, text: str) -> set[str]:
    return set(_refs_cached(lang, text))


@functools.lru_cache(maxsize=64)
def _parsed_units(lang: str, path: str, text: str):
    return funcsplit._units_strict(lang, path, text)


def _units(unit: Unit):
    """{qualname: (source, nested)} of the file, or (None, omission) when it cannot be read."""
    if unit.file_text is None:
        return None, "file_text_missing"
    try:
        units = _parsed_units(unit.language or "", unit.path, unit.file_text)
        if unit.language == "python" and getattr(units, "errors", 0):
            return None, "parse_failed"             # the splitter reads a Python file the standard parser rejects; a context is only built from a file that parses
        return units, None
    except funcsplit.Unavailable as e:
        reason = str(e)
        return None, f"unsupported_language:{unit.language}" if reason.startswith("no_grammar") else "parse_failed"


# -- imports and the paths they point to ---------------------------------------------------------------------------------------

def _py_import_specs(src: str) -> list[dict]:
    specs = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            specs += [{"module": a.name, "level": 0, "names": [], "text": ast.get_source_segment(src, node)} for a in node.names[:1]]
        elif isinstance(node, ast.ImportFrom):
            specs.append({"module": node.module, "level": node.level, "names": [a.name for a in node.names],
                          "text": ast.get_source_segment(src, node)})
    return sorted(specs, key=lambda s: src.find(s["text"] or ""))


def _py_candidates(path: str, spec: dict) -> list[str]:
    stems: list[str] = []
    if spec["level"]:
        d = posixpath.dirname(path)
        for _ in range(spec["level"] - 1):
            d = posixpath.dirname(d)
        if spec["module"]:
            stems.append(posixpath.join(d, *spec["module"].split(".")))
        else:                                                        # from . import x
            stems += [posixpath.join(d, n) for n in spec["names"]] + [d]
    else:
        mods = (spec["module"] or "").split(".")
        top = mods[0]
        for root in ("", "src/"):
            if path.startswith(f"{root}{top}/"):
                stems.append(posixpath.join(root, *mods))
    out = []
    for s in stems:
        s = posixpath.normpath(s) if s else s
        if s.startswith("..") or s == ".":
            continue
        out += [f"{s}.py", f"{s}/__init__.py"] if s and not s.endswith("__init__") else [f"{s}/__init__.py"]
    return list(dict.fromkeys(out))


def _js_candidates(path: str, spec: str) -> list[str]:
    if not spec.startswith(("./", "../")):
        return []                                                    # a package import is external
    stem = posixpath.normpath(posixpath.join(posixpath.dirname(path), spec))
    if stem.startswith(".."):
        return []
    if posixpath.splitext(stem)[1] in JS_EXTS:
        base, ext = posixpath.splitext(stem)
        return [stem] + ([f"{base}.ts", f"{base}.tsx"] if ext in (".js", ".jsx", ".mjs", ".cjs") else [])
    return [stem + e for e in JS_EXTS] + [f"{stem}/index{e}" for e in JS_EXTS]


def _absent(unit: Unit, path: str) -> bool:
    """True when the path is known not to exist at the parent commit (declared, or not in the listing)."""
    return path in unit.missing_paths or (unit.known_paths is not None and path not in unit.known_paths)


def _import_groups(unit: Unit) -> list[list[str]]:
    """For each local import of the file, its candidate paths (the first existing one is the module)."""
    if unit.file_text is None:
        return []
    groups: list[list[str]] = []
    if unit.language == "python":
        try:
            specs = _py_import_specs(unit.file_text)
        except SyntaxError:
            return []
        groups = [_py_candidates(unit.path, s) for s in specs]
    elif unit.language in JS_LANGS:
        found = [m.group(1) for m in _JS_IMPORT.finditer(unit.file_text)] + \
                [m.group(1) for m in _JS_REQUIRE.finditer(unit.file_text)]
        groups = [unit.resolution.get(s) or _js_candidates(unit.path, s) for s in dict.fromkeys(found)]
    return [g for g in groups if g and unit.path not in g]


def _manifest_candidates(unit: Unit) -> list[str]:
    """The primary manifest of the ecosystem in the nearest directories (a lock file is never wanted)."""
    a = ecosystems.get(unit.ecosystem)
    if not a:
        return []
    names = [n for n in a.manifest_files if not re.search(r"[*?\[]", n) and not _LOCKISH.search(n)]
    if not names:
        return []
    dirs, d = [], posixpath.dirname(unit.path)
    while d:
        dirs.append(d)
        d = posixpath.dirname(d)
    dirs = dirs[:3] + [""]
    return list(dict.fromkeys(posixpath.join(x, names[0]) for x in dirs))


def wanted_paths(unit: Unit) -> list[str]:
    """The files (at the parent commit) the builder would like, by path: the local modules the file imports
    and the nearest manifests. Fetching them is the caller's job; the order is stable and the list unique."""
    groups = _import_groups(unit)
    out = [p for g in groups for p in g] + _manifest_candidates(unit)
    if unit.known_paths is not None:
        out = [p for p in out if p in unit.known_paths]
        firsts = {}
        for g in groups:                                    # one module per import: the first candidate that exists
            hit = next((p for p in g if p in unit.known_paths), None)
            firsts.update({p: p == hit for p in g})
        out = [p for p in out if firsts.get(p, True)]
    return [p for p in dict.fromkeys(out) if p != unit.path]


# -- building ---------------------------------------------------------------------------------------------------------------------

def _fit(items: list[tuple], cap: int, remaining: int) -> tuple[list[ContextItem], int, bool]:
    """Take items in order while they fit the kind's cap and the total that is left."""
    out, used, trimmed = [], 0, False
    for kind, path, name, text, reason in items:
        if used + len(text) <= cap and len(text) <= remaining - used:
            out.append(ContextItem(kind, path, name, text, reason))
            used += len(text)
        else:
            trimmed = True
    return out, used, trimmed


def build(unit: Unit, budget: Budget = Budget()) -> Bundle:
    units, problem = _units(unit)
    if units is None:
        return Bundle([], "changed_files_only", [problem])
    omissions: list[str] = []
    # the target: a named function, else the function containing a hunk, else nothing to analyse
    target = unit.name if unit.name in units else None
    enclosing: list[tuple] = []
    if unit.name and target is None:
        omissions.append("unit_not_found")
    elif unit.name is None and util.norm_code(unit.code) != util.norm_code(unit.file_text or ""):
        holders = sorted((q for q, (src, _) in units.items() if util.norm_code(unit.code) in util.norm_code(src)),
                         key=lambda q: len(units[q][0]))
        if holders:
            target = holders[0]
            enclosing.append(("enclosing", unit.path, target, units[target][0], "the function containing the hunk"))
        else:
            omissions.append("enclosing_unit_unknown")

    callees, callers = [], []
    used_names = _refs(unit.language, unit.code)
    if target:
        t_leaf, t_src = _leaf(target), units[target][0]
        nested = f"{target.split('#')[0]}."
        others = [(q, s) for q, (s, _) in units.items() if q != target and not q.startswith(nested)]
        t_refs = _refs(unit.language, t_src)
        used_names |= t_refs
        for q, s in others:
            if _leaf(q) in t_refs:
                callees.append(("callee", unit.path, q, s, f"called by {target}"))
        for q, s in others:
            if t_leaf in _refs(unit.language, s):
                callers.append(("caller", unit.path, q, s, f"refers to {target}"))
        for _, _, _, s, _ in callees:
            used_names |= _refs(unit.language, s)
    if len(callees) > MAX_CALLEES:                    # a cap on the candidates is a trim too
        omissions.append("budget_trimmed:callees")
    if len(callers) > MAX_CALLERS:
        omissions.append("budget_trimmed:callers")
    callees, callers = callees[:MAX_CALLEES], callers[:MAX_CALLERS]

    # imports of the file, and the referenced definitions of the local modules it imports
    import_text = ""
    if unit.file_text is not None:
        if unit.language == "python":
            try:
                import_text = "\n".join(dict.fromkeys(s["text"] for s in _py_import_specs(unit.file_text) if s["text"]))
            except SyntaxError:
                pass
        else:
            import_text = "\n".join(dict.fromkeys(m.group(0).strip() for m in _IMPORT_LINE.finditer(unit.file_text)))
    imports = [("imports", unit.path, None, import_text, "the file's import statements")] if import_text else []
    modules: list[tuple] = []
    for group in _import_groups(unit):
        found = next((p for p in group if p in unit.blobs), None)
        if found is None:
            if not all(_absent(unit, p) for p in group):
                omissions.append(f"import_unfetched:{next(p for p in group if not _absent(unit, p))}")
            continue
        try:
            mod_units = funcsplit._units_strict(unit.language or "", found, unit.blobs[found])
        except funcsplit.Unavailable:
            omissions.append(f"module_unparsed:{found}")
            continue
        for q, (s, _) in mod_units.items():
            if _leaf(q) in used_names and "." not in q:
                modules.append(("module_symbol", found, q, s, f"imported by {unit.path}"))
    manifest: list[tuple] = []
    for path in sorted(unit.blobs):
        if path.endswith(('tsconfig.json', 'jsconfig.json')):
            manifest.append(('configuration', path, None, unit.blobs[path], 'compiler resolution settings at this snapshot'))
    cands = _manifest_candidates(unit)
    if cands:
        hit = next((p for p in cands if p in unit.blobs), None)
        if hit:
            manifest.append(("manifest", hit, None, unit.blobs[hit], "nearest dependency manifest"))
        elif not all(_absent(unit, p) for p in cands):
            omissions.append("manifest_unavailable")

    # fit by priority into the budgets
    remaining = budget.total
    items: list[ContextItem] = []
    for label, group, cap in (("enclosing", enclosing, budget.enclosing), ("callees", callees, budget.callees),
                              ("imports", imports, budget.imports), ("modules", modules, budget.modules),
                              ("callers", callers, budget.callers), ("config", manifest, budget.config)):
        if label == "imports" and group and len(group[0][3]) > min(cap, remaining):    # keep the head of a long list
            kind, p, n, text, why = group[0]
            lines, kept = text.splitlines(), []
            for line in lines:
                if sum(len(x) + 1 for x in kept) + len(line) + 1 > min(cap, remaining):
                    break
                kept.append(line)
            group = [(kind, p, n, "\n".join(kept), why)] if kept else []
            omissions.append("budget_trimmed:imports")
        if label == "config" and group and len(group[0][3]) > min(cap, remaining):
            kind, p, n, text, why = group[0]
            group = [(kind, p, n, text[:max(min(cap, remaining), 0)], why)] if min(cap, remaining) > 0 else []
            omissions.append("budget_trimmed:config")
        taken, used, trimmed = _fit(group, cap, remaining)
        items += taken
        remaining -= used
        if trimmed:
            omissions.append(f"budget_trimmed:{label}")
    return Bundle(items, "partial" if omissions else "complete", list(dict.fromkeys(omissions)))


def build_oracle(unit: Unit, *, diff: str, advisory: str, budget: Budget = Budget()) -> Bundle:
    """The standard bundle plus the fix diff and the advisory, as a labelled diagnostic: it measures how much
    the answer depends on facts a model would not have, and is never allowed in the main evaluation."""
    base = build(unit, budget)
    extra = [ContextItem("oracle_diff", unit.path, None, diff, "the fix diff (diagnostic only)"),
             ContextItem("oracle_advisory", unit.path, None, advisory, "the advisory text (diagnostic only)")]
    return Bundle(base.items + extra, base.completeness, base.omissions, ORACLE_POLICY, "oracle_diagnostic", False)


# -- storage in the derived store -----------------------------------------------------------------------------------------------------------

def save(conn, record_id: str, bundle: Bundle) -> str:
    """Keep the bundle once by its hash and point the record at it."""
    h = bundle.bundle_hash
    from app.backend.pipeline import derived
    mg = derived.mongo_of(conn)
    mg.bundle_save(conn, record_id, h, bundle.policy, bundle.variant, bundle.completeness, bundle.to_dict())
    return h


def load(conn, bundle_hash: str) -> Bundle | None:
    from app.backend.pipeline import derived
    mg = derived.mongo_of(conn)
    body = mg.bundle_body(conn, bundle_hash)
    return Bundle.from_dict(body) if body else None


def bundle_of(conn, record_id: str) -> str | None:
    from app.backend.pipeline import derived
    mg = derived.mongo_of(conn)
    return mg.bundle_of(conn, record_id)
