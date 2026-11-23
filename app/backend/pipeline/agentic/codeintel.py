"""Finding a symbol: where it is defined, where it is used, who imports it.

A small model that is asked to rename a function or to change a signature spends rounds on ``grep`` and on reading files to tell the definition from the calls from the imports. ``find_symbol`` does
that in one call. This module is the pure part (text in, hits out; no file system, no sandbox), so that it can be tested on its own:

* Python files are parsed with the standard ``ast`` module, so a name in a string or a comment is not a hit, a call is told from a plain use, and each hit says which function or class it is in.
  A file that does not parse (half-written code is exactly what an agent works on) falls back to the text scan below.
* Every other language is read line by line: a line that holds the name as a whole word is a definition, an import, a call or a use by the shape of the line. That is approximate and says so
  (``approximate: true`` on the file).

The adapters that find the files (the sandbox copy in Manual and Auto, the project on disk in Plan) are in ``codeintel_tools.py``.
"""
from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass, field

LINE_CHARS = 160
MAX_SCANNED_LINE = 2_000           # a longer line is minified or generated: it is not read for symbols (and a pattern cannot be made to run long on it)
MAX_DEFINITIONS = 25
MAX_USES = 60
MAX_IMPORTS = 25
PY_SUFFIXES = (".py", ".pyi")

_MODIFIERS = r"(?:export\s+|default\s+|public\s+|private\s+|protected\s+|internal\s+|static\s+|async\s+|final\s+|abstract\s+|override\s+|pub(?:\([a-z]+\))?\s+|unsafe\s+|extern\s+|inline\s+|virtual\s+|sealed\s+|open\s+|data\s+|suspend\s+)*"
_KEYWORDS = r"(?:function\*?|class|interface|enum|struct|trait|type|fn|func|def|fun|const|let|var|val|object|module|impl|record|protocol|namespace|package|sub|proc)"
_IMPORT_LINE = re.compile(r"^\s*(?:import\b|from\b.+\bimport\b|use\b|using\b|require\b|extern\s+crate\b|#\s*include\b|@import\b|load\()|\brequire\s*\(|\bimport\s*\(|\bimport\s+.{1,200}?\s+from\b")


@dataclass
class Hit:
    file: str
    line: int
    kind: str                       # definition | import | call | use | parameter | keyword
    text: str
    scope: str = ""                 # the function or class the hit is in
    detail: str = ""                # for a definition: what it is (function, class, variable ...)

    def to_dict(self) -> dict:
        out = {"file": self.file, "line": self.line, "kind": self.kind, "text": self.text}
        if self.scope:
            out["in"] = self.scope
        if self.detail:
            out["is"] = self.detail
        return out


@dataclass
class FileHits:
    file: str
    hits: list[Hit] = field(default_factory=list)
    approximate: bool = False
    parsed: bool = False


def split_name(name: str) -> tuple[str, str]:
    """``(owner, last)`` of a name the model gave: ``Cart.total`` is ``("Cart", "total")``, ``total`` is ``("", "total")``. Only identifiers are accepted."""
    parts = [p for p in re.split(r"[.:]+|->|::", str(name or "").strip()) if p]
    if not parts or not all(re.fullmatch(r"[A-Za-z_$][\w$]*", p) for p in parts):
        raise ValueError("Give one name: a function, class, variable or method, for example load_user or Cart.total.")
    return (parts[-2] if len(parts) > 1 else ""), parts[-1]


def word_in(text: str, name: str) -> bool:
    return re.search(rf"(?<![\w$]){re.escape(name)}(?![\w$])", text) is not None


def _line_text(lines: list[str], n: int) -> str:
    raw = lines[n - 1] if 0 < n <= len(lines) else ""
    raw = raw.strip()
    return raw if len(raw) <= LINE_CHARS else raw[:LINE_CHARS - 3] + "..."


class _Walker(ast.NodeVisitor):
    def __init__(self, path: str, name: str, owner: str, lines: list[str]):
        self.path, self.name, self.owner, self.lines = path, name, owner, lines
        self.stack: list[tuple[str, str]] = []             # (kind, name) of the classes and functions around the node
        self.hits: list[Hit] = []
        self._seen: set[tuple[int, str]] = set()

    def _scope(self) -> str:
        return ".".join(n for _, n in self.stack)

    def _add(self, line: int, kind: str, detail: str = "", inside: str = "") -> None:
        key = (line, kind)
        if key in self._seen:
            return
        self._seen.add(key)
        scope = ".".join(p for p in (self._scope(), inside) if p)
        self.hits.append(Hit(self.path, line, kind, _line_text(self.lines, line), scope, detail))

    def _owner_ok(self) -> bool:
        """With ``Cart.total`` asked for, a definition counts only inside a class called Cart."""
        if not self.owner:
            return True
        return any(k == "class" and n == self.owner for k, n in self.stack)

    def _define(self, node, kind: str) -> None:
        if node.name == self.name and self._owner_ok():
            self._add(node.lineno, "definition", "class" if kind == "class" else ("method" if self.stack and self.stack[-1][0] == "class" else "function"))
        self.stack.append((kind, node.name))
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node):
        for a in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs, *([node.args.vararg] if node.args.vararg else []), *([node.args.kwarg] if node.args.kwarg else [])]:
            if a.arg == self.name and not self.owner:
                self._add(a.lineno, "parameter", inside=node.name)
        for d in [*node.decorator_list, *([node.returns] if node.returns else [])]:
            self.visit(d)
        self._define(node, "function")

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        for d in [*node.decorator_list, *node.bases, *node.keywords]:
            self.visit(d)
        self._define(node, "class")

    def _targets(self, node, targets) -> None:
        if self.owner and not self._owner_ok():
            return
        for t in targets:
            for sub in ast.walk(t):
                if isinstance(sub, ast.Name) and sub.id == self.name and isinstance(sub.ctx, ast.Store):
                    where = "variable" if not self.stack or self.stack[-1][0] == "class" else None
                    if where:
                        self._add(sub.lineno, "definition", "constant" if self.name.isupper() else where)
                    else:
                        self._add(sub.lineno, "use")

    def visit_Assign(self, node):
        self._targets(node, node.targets)
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        self._targets(node, [node.target])
        self.generic_visit(node)

    def visit_Call(self, node):
        f = node.func
        if isinstance(f, ast.Name) and f.id == self.name and not self.owner:
            self._add(node.lineno, "call")
        elif isinstance(f, ast.Attribute) and f.attr == self.name:       # obj.name(...): the type of obj is not known, so a call of a method of that name is listed whichever class it is of
            self._add(node.lineno, "call")
        self.generic_visit(node)

    def visit_Name(self, node):
        if node.id == self.name and not self.owner and not isinstance(node.ctx, ast.Store):
            self._add(node.lineno, "use")

    def visit_Attribute(self, node):
        if node.attr == self.name:
            self._add(node.lineno, "use")
        self.generic_visit(node)

    def visit_keyword(self, node):
        if node.arg == self.name and not self.owner:
            self._add(getattr(node.value, "lineno", 0) or 0, "keyword")
        self.generic_visit(node)

    def visit_Import(self, node):
        for a in node.names:
            if a.name.split(".")[-1] == self.name or a.asname == self.name:
                self._add(node.lineno, "import")

    def visit_ImportFrom(self, node):
        for a in node.names:
            if a.name == self.name or a.asname == self.name or (node.module or "").split(".")[-1] == self.name:
                self._add(node.lineno, "import")


def analyze_python(text: str, path: str, name: str, owner: str = "") -> FileHits | None:
    """The hits for ``name`` in one Python file, or None when the file does not parse."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return None
    walker = _Walker(path, name, owner, text.splitlines())
    try:
        walker.visit(tree)
    except RecursionError:
        return None
    # a call and the name it calls are one hit, not two
    calls = {h.line for h in walker.hits if h.kind == "call"}
    hits = [h for h in walker.hits if not (h.kind == "use" and h.line in calls)]
    return FileHits(path, sorted(hits, key=lambda h: (h.line, h.kind)), approximate=False, parsed=True)


def analyze_text(text: str, path: str, name: str, owner: str = "") -> FileHits:
    """The hits for ``name`` in a file read line by line (any language, and Python that does not parse)."""
    word = rf"(?<![\w$]){re.escape(name)}(?![\w$])"
    define = re.compile(rf"^\s*{_MODIFIERS}{_KEYWORDS}\s+{word}")
    method = re.compile(rf"^\s*{_MODIFIERS}(?:[\w<>\[\],.?*&]+\s+)+{word}\s*\([^;]*$")
    call = re.compile(rf"{word}\s*\(")
    lines = text.splitlines()
    out = FileHits(path, approximate=True)
    for n, line in enumerate(lines, 1):
        if len(line) > MAX_SCANNED_LINE or not re.search(word, line):
            continue
        stripped = line.strip()
        if stripped.startswith(("#", "//", "*", "/*")) and not _IMPORT_LINE.match(line):
            continue                                                 # a comment line is not a use
        if _IMPORT_LINE.search(line):
            kind, detail = "import", ""
        elif define.match(line) or method.match(line):
            kind, detail = "definition", ""
        elif call.search(line):
            kind, detail = "call", ""
        else:
            kind, detail = "use", ""
        out.hits.append(Hit(path, n, kind, _line_text(lines, n), "", detail))
    return out


def analyze(text: str, path: str, name: str, owner: str = "") -> FileHits | None:
    """The hits in one file, or None when the name is not in it as a word."""
    if not word_in(text, name):
        return None
    if os.path.splitext(path)[1].lower() in PY_SUFFIXES:
        parsed = analyze_python(text, path, name, owner)
        if parsed is not None:
            return parsed if parsed.hits else None
    found = analyze_text(text, path, name, owner)
    return found if found.hits else None


def collect(files: list[FileHits], name: str, kind: str = "all") -> dict:
    """The result of ``find_symbol``: definitions first, then imports, then the places that use it. Each list is cut at its own limit and the cut is said."""
    defs, imports, uses = [], [], []
    approximate = []
    for fh in files:
        if fh.approximate:
            approximate.append(fh.file)
        for h in fh.hits:
            (defs if h.kind == "definition" else imports if h.kind == "import" else uses).append(h)
    out: dict = {"name": name}
    if kind in ("all", "definition"):
        out["definitions"] = [h.to_dict() for h in defs[:MAX_DEFINITIONS]]
    if kind in ("all", "import"):
        out["imports"] = [h.to_dict() for h in imports[:MAX_IMPORTS]]
    if kind in ("all", "use"):
        out["uses"] = [h.to_dict() for h in uses[:MAX_USES]]
    cut = [f"{label}: {len(rows) - limit} more" for label, rows, limit in (("definitions", defs, MAX_DEFINITIONS), ("imports", imports, MAX_IMPORTS), ("uses", uses, MAX_USES)) if len(rows) > limit]
    if cut:
        out["truncated"] = "Not shown (" + "; ".join(cut) + "). Give a more exact name (Class.method) or a path."
    if approximate:
        out["approximate"] = f"{len(approximate)} file(s) were read line by line, not parsed, so a hit may be a different thing of the same name: " + ", ".join(approximate[:5]) + ("..." if len(approximate) > 5 else "")
    total = len(defs) + len(imports) + len(uses)
    files_with = len([f for f in files if f.hits])
    out["summary"] = f"Found {name}: {len(defs)} definition(s), {len(imports)} import(s), {len(uses)} use(s) in {files_with} file(s)" if total else f"No hit for {name} in {len(files)} file(s) read"
    return out
