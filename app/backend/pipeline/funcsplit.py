"""Split a changed source file into function-level (or hunk-level) code pairs, and say what became of every changed line.

A whole-file pair is a poor training sample: the vulnerable lines are a small
part of it, and long files are dropped by the line cap. For every changed file the
miner (and the "Re-split stored pairs" stage) asks this module for

* **function pairs** - each function / method present in both versions whose
  code changed, found with a real parser: Python's ``ast`` for Python and
  tree-sitter grammars (``tree-sitter-language-pack``) for JavaScript, TypeScript,
  Java, Kotlin, Go, C, C++, C#, Ruby, PHP, Rust, Swift and Scala (every ``<script>``
  block of Vue / Svelte files). Functions are matched by qualified name
  (``Class.method``; nested functions under their parent; the n-th repeat of a
  name - overloads, property setters - gets ``#n``). Python functions inside
  ``if`` / ``try`` / ``with`` / ``for`` / ``while`` / ``match`` blocks are found too.
  A function whose change lies only inside a nested function is not repeated: the
  nested one is the pair. A function renamed with a code change is matched to its
  old name (``function_before``) by body similarity.
* **hunk pairs** - when the language has no grammar, the parse fails, or no
  function changed (top-level code): each changed region with ``CONTEXT`` lines
  around it, nearby regions merged.

A change that only touches comments or formatting is not a change (``is_noop``). ``split_with_status`` also says
how the extraction went (``ok``, ``no_grammar``, ``fallback`` with a reason such as
``python_syntax_error`` or ``grammar_unavailable:<lang>``) instead of falling back silently.

Grammars come with tree-sitter-language-pack. A parser is created once per thread; a grammar that could not be loaded is tried again after ``RETRY_AFTER`` seconds, so a
transient failure does not turn into a fallback for the rest of the process. What the parse of a source produced (its units with their spans, signatures and syntax-tree hashes) is kept in a
bounded cache addressed by the content, the language and the parser versions, so a file is parsed once however many callers ask.

Since version 6 ``analyze`` is the entry point and ``split``, ``split_with_status`` and the older helpers
are projections of it:

* every changed line is accounted for (``Analysis.coverage``): it is in a pair, in a function with no counterpart, in a function that only moved or was renamed, in a residual region outside
  every function, or a comment or blank. A change outside every function (imports, constants, class members, annotations, types) is a hunk pair with the role ``residual``;
* a function is matched to its other version by ``splitmatch`` (name, signature, body, position), so renames, moves, overloads, splits and merges are told apart; every match has a kind, a
  confidence and its alternatives, and a pairing that is a guess is marked as one;
* equality is decided by the syntax tree (``is_noop``), not by token text;
* a file with syntax errors still gives the functions that parse; Python that ``ast`` rejects is read with the tree-sitter grammar, and when only one version needs it both are (``parse_pair``);
* each pair carries its provenance (lines, byte offsets, signature, parser), an edit script of the function (``astdiff``) and its dependency context (``splitdeps``, read from the vulnerable
  version only); ``link_cross_file`` links a function that left one file and arrived in another;
* sources over ``MAX_SOURCE_BYTES`` are not parsed and the work per file is bounded; a larger input is a recorded fallback, never a silent loss.
"""
from __future__ import annotations
import ast
import bisect
import collections
import difflib
import hashlib
import importlib.metadata
import io
import re
import threading
import time
import tokenize
from dataclasses import dataclass

from app.backend.pipeline.pyparse import parse_python
from app.backend.pipeline import astdiff, splitdeps, splitmatch, util

VERSION = 6                  # bump when splitting changes: stored pairs are re-split once
CONTEXT = 15                 # lines of context around a changed region (hunk pairs)
MAX_UNITS = 2000             # parse result sanity cap per file
MAX_SOURCE_BYTES = 2_000_000 # a larger source (in bytes) is not parsed, compared as a tree or hashed (hunk fallback with the reason source_too_large)
MAX_RECOVERY = 6             # most broken regions blanked and parsed again in one source
RECOVERY_MAX_ERRORS = 60     # a parse with more error nodes than this is not repaired (the grammar does not read the language of the file)
MAX_UNIT_CHARS = 6_000_000   # the text of all units of one version together: nested functions repeat their children's text, so a deep chain holds depth x size characters
CACHE_BYTES = 48 * 2 ** 20   # the parse cache holds at most about this much source text
CACHE_ENTRIES = 1024
RETRY_AFTER = 60.0           # seconds before a grammar that failed to load is tried again
# statements a broken parse reads as methods (`if (x) {}` in a class body); a method may really be called that, so they are refused only when the file did not parse cleanly
_CONTROL_WORDS = {"if", "for", "while", "switch", "catch", "with", "return"}

# per language: tree-sitter grammar, function node types, container node types
_TS = {
    "javascript": ("javascript", {"function_declaration", "generator_function_declaration",
                                  "method_definition"},
                   {"class_declaration", "class"}),
    "typescript": ("typescript", {"function_declaration", "generator_function_declaration",
                                  "method_definition", "function_signature"},
                   {"class_declaration", "abstract_class_declaration", "class",
                    "internal_module", "module"}),
    "java": ("java", {"method_declaration", "constructor_declaration", "compact_constructor_declaration"},
             {"class_declaration", "interface_declaration", "enum_declaration", "record_declaration"}),
    "kotlin": ("kotlin", {"function_declaration", "secondary_constructor"},
               {"class_declaration", "object_declaration", "companion_object"}),
    "scala": ("scala", {"function_definition"},
              {"class_definition", "object_definition", "trait_definition"}),
    "go": ("go", {"function_declaration", "method_declaration"}, set()),
    "c": ("c", {"function_definition"}, set()),
    "cpp": ("cpp", {"function_definition"},
            {"class_specifier", "struct_specifier", "namespace_definition"}),
    "csharp": ("csharp", {"method_declaration", "constructor_declaration", "local_function_statement",
                          "operator_declaration", "destructor_declaration"},
               {"class_declaration", "struct_declaration", "interface_declaration",
                "namespace_declaration", "record_declaration"}),
    "ruby": ("ruby", {"method", "singleton_method"}, {"class", "module"}),
    "php": ("php", {"function_definition", "method_declaration"},
            {"class_declaration", "trait_declaration", "interface_declaration"}),
    "rust": ("rust", {"function_item"}, {"impl_item", "trait_item", "mod_item"}),
    "swift": ("swift", {"function_declaration", "init_declaration"},
              {"class_declaration", "protocol_declaration"}),
}
_TS["tsx"] = ("tsx", _TS["typescript"][1], _TS["typescript"][2])
_TS["python"] = ("python", {"function_definition"}, {"class_definition"})        # only for sources that the standard `ast` parser rejects
_JS_FUNC_VALUES = {"arrow_function", "function_expression", "function", "generator_function"}
_SCRIPT_OPEN = re.compile(r"<script\b([^<>]*)>", re.I)            # no `<` inside the tag: a run of unclosed `<script` openers is scanned once, not once per opener
_HTML_COMMENT = re.compile(r"<!--(?:(?!-->).)*(?:-->|\Z)", re.S)    # an unclosed comment runs to the end and is one match
_LINE_END = re.compile(r"\r\n|\r|\n")


# ── parsers: one per thread, retried after a failure ─────────────────────────

def _versions() -> dict[str, str]:
    out = {}
    for dist in ("tree-sitter", "tree-sitter-language-pack", "tree-sitter-javascript", "tree-sitter-typescript"):
        try:
            out[dist] = importlib.metadata.version(dist)
        except importlib.metadata.PackageNotFoundError:
            out[dist] = "unknown"
    return out


PARSERS = _versions()
_local = threading.local()
_parsers_lock = threading.Lock()
_failed: dict[str, float] = {}               # grammar -> when loading it failed
_generation = 0                              # bumped by _reset_parsers: a thread drops its parsers when it sees a newer one
_clock = time.monotonic


class Unavailable(Exception):
    """The file cannot be split into functions; the message is the reason."""


def parser_fingerprint() -> str:
    """The parser stack in one string (tree-sitter and the grammar pack); stored with what was split so a changed grammar is noticed."""
    return (f"ts{PARSERS['tree-sitter']}+tslp{PARSERS['tree-sitter-language-pack']}"
            f"+js{PARSERS.get('tree-sitter-javascript', 'unknown')}+typescript{PARSERS.get('tree-sitter-typescript', 'unknown')}")


def _reset_parsers() -> None:
    """Forget every loaded parser and every remembered failure (tests, and after the grammar pack was replaced)."""
    global _generation
    with _parsers_lock:
        _failed.clear()
        _generation += 1


TREE_MEMO = 6                # recent parses a thread keeps, so the units, the edit scripts, the dependency context and the line kinds of one analysis read one tree
TREE_MEMO_BYTES = 600_000    # most source bytes the kept parses stand for (a tree is many times its source in memory)


def _parse_memo(grammar: str, parser, data: bytes):
    """The syntax tree of ``data`` for ``grammar``, parsed once while it is among the last few parses of this thread. A tree is never edited here, so sharing it is safe."""
    if len(data) > TREE_MEMO_BYTES // 2:
        return parser.parse(data)
    memo = getattr(_local, "trees", None)
    if memo is None or getattr(_local, "trees_generation", None) != _generation:
        memo = _local.trees = collections.OrderedDict()
        _local.trees_generation = _generation
    key = (grammar, len(data), hash(data))
    hit = memo.get(key)
    if hit is not None and hit[0] == data:
        memo.move_to_end(key)
        return hit[1]
    tree = parser.parse(data)
    memo[key] = (data, tree)
    while len(memo) > TREE_MEMO or sum(len(d) for d, _ in memo.values()) > TREE_MEMO_BYTES:
        memo.popitem(last=False)
    return tree


def _installed_npm_parser(grammar: str):
    from tree_sitter import Language, Parser
    if grammar == 'javascript':
        import tree_sitter_javascript
        language = tree_sitter_javascript.language()
    else:
        import tree_sitter_typescript
        language = (tree_sitter_typescript.language_tsx() if grammar == 'tsx'
                    else tree_sitter_typescript.language_typescript())
    return Parser(Language(language))


def _parser(grammar: str):
    """A tree-sitter parser for ``grammar`` (one per thread: a parser is not thread-safe), or None. A grammar that failed to load is not asked for again before ``RETRY_AFTER`` seconds."""
    mine = getattr(_local, "parsers", None)
    if mine is None or getattr(_local, "generation", None) != _generation:
        mine = _local.parsers = {}
        _local.generation = _generation
    p = mine.get(grammar)
    if p is not None:
        return p
    failed_at = _failed.get(grammar)
    if failed_at is not None and _clock() - failed_at < RETRY_AFTER:
        return None
    with _parsers_lock:                       # the first load may download the grammar
        try:
            if grammar in ('javascript', 'typescript', 'tsx'):
                # The npm pipeline has pinned offline grammars. A remote
                # language-pack manifest must never be a runtime prerequisite.
                p = _installed_npm_parser(grammar)
            else:
                import tree_sitter_language_pack as tslp
                p = tslp.get_parser(grammar)
        except MemoryError:
            raise
        except Exception:  # ignored: the grammar is remembered as unavailable for a while and the file falls back to whole-file pairs
            _failed[grammar] = _clock()
            return None
        _failed.pop(grammar, None)
    mine[grammar] = p
    return p


# ── comparing code without comments and formatting (lexical; the syntax-tree comparison is below) ──

_C_LIKE = {"javascript", "typescript", "java", "kotlin", "scala", "go", "c", "cpp", "csharp", "rust", "swift",
           "php", "vue", "svelte", "dart", "solidity", "objective-c", "groovy", "ejs", "handlebars"}
_HASH = {"ruby", "shell", "perl", "elixir", "r", "yaml"}
_PUNCT = set("{}()[];,:=+-*/<>!&|?.%^~@")


_NOSPACE = set("()[]{},;")        # whitespace next to these is only layout; between other characters it may separate two tokens (`a - -b` is not `a--b`)


def _strip_comments(code: str, line: str | None, block: tuple[str, str] | None) -> str:
    """Remove comments (outside string literals) and collapse runs of whitespace outside strings to one space, dropped next to brackets, commas and semicolons.

    This is the lexical fallback for text that has no syntax tree (a hunk, a language without a parser); it errs towards "different": two tokens are never glued together."""
    out: list[str] = []
    i, n = 0, len(code)
    pending = False
    while i < n:
        c = code[i]
        if c in "\"'`":
            q, j = c, i + 1
            while j < n and code[j] != q:
                j += 2 if code[j] == "\\" else 1
            if pending and out and out[-1][-1:] not in _NOSPACE:
                out.append(" ")
            pending = False
            out.append(code[i:j + 1])
            i = j + 1
            continue
        if line and code.startswith(line, i):
            j = code.find("\n", i)
            i = n if j < 0 else j
            pending = True
            continue
        if block and code.startswith(block[0], i):
            j = code.find(block[1], i + len(block[0]))
            i = n if j < 0 else j + len(block[1])
            pending = True
            continue
        if c.isspace():
            pending = True
            i += 1
            continue
        if pending and out and out[-1][-1:] not in _NOSPACE and c not in _NOSPACE:
            out.append(" ")
        pending = False
        out.append(c)
        i += 1
    return "".join(out)


def _py_canonical(code: str) -> str:
    """The token stream of Python code without comments and blank lines (indentation kept)."""
    try:
        toks = [f"{t.type}:{t.string}" for t in tokenize.generate_tokens(io.StringIO(code).readline)
                if t.type not in (tokenize.COMMENT, tokenize.NL)]
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return util.norm_code(code)
    return "\x1f".join(toks)


def canonical(lang: str | None, code: str | None) -> str:
    """A form of ``code`` that ignores comments and formatting where the language is known;
    elsewhere only trailing whitespace is ignored."""
    code = code or ""
    if lang == "python":
        return _py_canonical(code)
    if lang in _C_LIKE:
        return _strip_comments(code, "//", ("/*", "*/")).strip()
    if lang in _HASH:
        return _strip_comments(code, "#", None).strip()
    return util.norm_code(code)


_WORD = re.compile(r"[A-Za-z_][A-Za-z_0-9]*|\d+|\S")


def tokens(lang: str | None, code: str | None) -> list[str]:
    if len(code or '') < 16384:
        return _tokens_uncached(lang, code)
    from app.backend.pipeline.acceleration.cache import default
    from app.backend.pipeline.acceleration.analysis_cache import stack_fingerprint
    key = [stack_fingerprint(), lang, hashlib.sha256((code or '').encode()).hexdigest()]
    return default().get_or_compute('tokens-v1', key, lambda: _tokens_uncached(lang, code))


def _tokens_uncached(lang: str | None, code: str | None) -> list[str]:
    """Source tokens without comments or layout, for similarity measures (identifiers are kept)."""
    code = code or ""
    if lang == "python":
        try:
            skip = (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT,
                    tokenize.ENDMARKER)
            return [t.string for t in tokenize.generate_tokens(io.StringIO(code).readline) if t.type not in skip]
        except (tokenize.TokenError, IndentationError, SyntaxError):
            return _WORD.findall(code)
    if lang in _C_LIKE:
        return _WORD.findall(_strip_comments(code, "//", ("/*", "*/")))
    if lang in _HASH:
        return _WORD.findall(_strip_comments(code, "#", None))
    return _WORD.findall(code)


# ── units: qualified name -> source ─────────────────────────────────────────

@dataclass(frozen=True)
class UnitInfo:
    """Where a unit is and what it looks like, beside its text (``_Units[key][0]``).

    ``lines`` are 1-based and inclusive, ``bytes`` index the UTF-8 encoding of the source, so ``source_bytes[bytes[0]:bytes[1]]`` is the unit's text exactly. ``shash`` is a hash of the
    unit's syntax tree without comments, ``ohash`` the same with the nested units left out (it tells whether the unit's own code changed), ``nhash`` the same as ``shash`` with the unit's
    own name left out (two units with one ``nhash`` are the same code under two names)."""
    key: str                  # unique within the version: qual, or qual#n for the n-th repeat of a name
    qual: str                 # qualified name without #n
    name: str                 # last component of qual
    kind: str                 # syntax node type of the function
    scope: str                # qualified name of what encloses it ("" at the top)
    parent: str | None        # key of the enclosing unit when it is nested in a function
    signature: str            # the parameter list, whitespace collapsed
    arity: int | None
    lines: tuple[int, int]
    bytes: tuple[int, int]
    shash: str
    ohash: str
    nhash: str = ""
    block: int = 0            # which <script> block of a Vue / Svelte file


class _Units(dict):
    """The units of one version of a file: ``{key: (source, [nested keys])}``, with ``info`` ({key: UnitInfo}) beside it. ``errors`` counts the ERROR and MISSING nodes of the parse they came
    from (0: a clean parse; the units are then complete), ``error_lines`` says where (1-based, inclusive pairs, at most 40). Shared through the cache: never modified after it is built."""
    errors = 0
    grammar = ""
    engine = "tree"                           # "ast": a Python file read with the standard ``ast`` (clean syntax); "tree": read with a tree-sitter grammar. Hashes of the two do not compare.

    def __init__(self):
        super().__init__()
        self.info: dict[str, UnitInfo] = {}
        self.error_lines: list[tuple[int, int]] = []
        self._chars = 0                       # the characters of all unit texts so far (bounded by MAX_UNIT_CHARS)
        self.recovered = 0                    # broken regions that were blanked so the rest could be read


def _hex(h) -> str:
    return h.hexdigest()


def _line_table(src: str) -> tuple[list[int], list[int]]:
    """Character offsets of where each line starts and where its text ends (before the terminator), with Python's own idea of a line (``\\n``, ``\\r\\n``, ``\\r``: not a form feed)."""
    starts, ends = [0], []
    for m in _LINE_END.finditer(src):
        ends.append(m.start())
        starts.append(m.end())
    ends.append(len(src))
    return starts, ends


def _py_dump(node, root, skip_nested: bool, mask_name: bool = False) -> str:
    """A string for a syntax tree that does not depend on layout; nested functions are left out when ``skip_nested`` and the root function's own name when ``mask_name``."""
    if isinstance(node, ast.AST):
        if skip_nested and node is not root and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return "<nested>"
        parts = ["<name>" if (mask_name and node is root and f == "name") else _py_dump(getattr(node, f, None), root, skip_nested, mask_name) for f in node._fields]
        return type(node).__name__ + "(" + ",".join(parts) + ")"
    if isinstance(node, list):
        return "[" + ",".join(_py_dump(x, root, skip_nested, mask_name) for x in node) + "]"
    return repr(node)


def _blocks(node):
    """Statement lists nested in a compound statement (not in a function or class)."""
    for attr in ("body", "orelse", "finalbody"):
        v = getattr(node, attr, None)
        if isinstance(v, list):
            yield v
    for h in getattr(node, "handlers", None) or []:
        yield h.body
    for c in getattr(node, "cases", None) or []:
        yield c.body


def _python_units(src: str) -> _Units:
    tree = parse_python(src)
    starts, ends = _line_table(src)
    ascii_only = src.isascii()
    byte_start = None if ascii_only else [0]
    if byte_start is not None:
        for i in range(1, len(starts)):
            byte_start.append(byte_start[-1] + len(src[starts[i - 1]:starts[i]].encode("utf-8")))
    units = _Units()
    units.grammar = "python"
    units.engine = "ast"
    seen: dict[str, int] = {}
    nodes: dict[str, tuple] = {}

    def add(qual: str, node, parent: str | None, prefix: str) -> str:
        seen[qual] = seen.get(qual, 0) + 1
        key = qual if seen[qual] == 1 else f"{qual}#{seen[qual]}"
        first = min([node.lineno, *(d.lineno for d in node.decorator_list)])
        last = node.end_lineno
        lo, hi = starts[first - 1], ends[last - 1]
        b0 = starts[first - 1] if ascii_only else byte_start[first - 1]
        b1 = b0 + (hi - lo if ascii_only else len(src[lo:hi].encode("utf-8")))
        nodes[key] = (node, lo, hi)
        args = node.args
        n_args = len(args.posonlyargs) + len(args.args) + len(args.kwonlyargs) + bool(args.vararg) + bool(args.kwarg)
        units._chars += hi - lo
        if units._chars > MAX_UNIT_CHARS:
            raise Unavailable("units_too_large")
        units[key] = (src[lo:hi], [])
        units.info[key] = UnitInfo(key, qual, node.name, "async_function_definition" if isinstance(node, ast.AsyncFunctionDef) else "function_definition",
                                   prefix.rstrip("."), parent, "(" + ast.unparse(args) + ")", n_args, (first, last), (b0, b1), "", "", "")
        if parent and parent in units:
            units[parent][1].append(key)
        return key

    def visit(body, prefix: str, parent: str | None):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                key = add(prefix + node.name, node, parent, prefix)
                visit(node.body, units.info[key].qual + ".", key)         # nested functions
            elif isinstance(node, ast.ClassDef):
                visit(node.body, prefix + node.name + ".", parent)
            else:
                for block in _blocks(node):                               # if / try / with / for / while / match
                    visit(block, prefix, parent)
    visit(tree.body, "", None)
    memo: dict[int, str] = {}
    for key, (node, lo, hi) in nodes.items():
        try:
            full = _py_dump_memo(node, memo)
            shash = _blake(full)
            nhash = _blake(_mask_py_name(full, node))
            ohash = _blake(_py_dump(node, node, True)) if units[key][1] else shash        # no nested function: the own code is all of it
        except RecursionError:                      # a tree too deep to walk: compare its text instead
            shash = ohash = nhash = _blake(src[lo:hi])
        units.info[key] = _with_hashes(units.info[key], shash, ohash, nhash)
    return units


def _blake(text: str) -> str:
    return hashlib.blake2b(text.encode("utf-8"), digest_size=10).hexdigest()


def _py_dump_memo(node, memo: dict[int, str]) -> str:
    """``_py_dump`` of a syntax tree, remembering the dump of every function so a nested function is dumped once, not once per enclosing function."""
    if isinstance(node, ast.AST):
        is_fn = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        if is_fn and id(node) in memo:
            return memo[id(node)]
        out = type(node).__name__ + "(" + ",".join(_py_dump_memo(getattr(node, f, None), memo) for f in node._fields) + ")"
        if is_fn:
            memo[id(node)] = out
        return out
    if isinstance(node, list):
        return "[" + ",".join(_py_dump_memo(x, memo) for x in node) + "]"
    return repr(node)


def _mask_py_name(dump: str, node) -> str:
    """A function's dump with its own name left out (the name is the first field)."""
    head = type(node).__name__ + "(" + repr(node.name)
    return type(node).__name__ + "(<name>" + dump[len(head):] if dump.startswith(head) else dump


def _text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _name_node(node):
    """The syntax node that holds the name of a function or container, or None (a Swift ``init`` has none)."""
    n = node.child_by_field_name("name")
    if n is None and node.type in ("function_definition",):       # C / C++: the declarator
        d = node.child_by_field_name("declarator")
        while d is not None and d.child_by_field_name("declarator") is not None \
                and d.type not in ("identifier", "field_identifier", "qualified_identifier",
                                   "destructor_name", "operator_name"):
            d = d.child_by_field_name("declarator")
        n = d
    if n is None and node.type == "impl_item":
        n = node.child_by_field_name("type")
    if n is None and node.type in ("init_declaration",):
        return None
    if n is None:
        for c in node.named_children:
            if c.type in ("identifier", "type_identifier", "simple_identifier", "constant",
                          "property_identifier", "name"):
                return c
    return n


def _name(node, src: bytes) -> str | None:
    if node.type in ("init_declaration",) and node.child_by_field_name("name") is None:
        return "init"
    n = _name_node(node)
    if n is None:
        return None
    return re.sub(r"\s+", "", _text(n, src))[:120] or None


def _error_count(root) -> int:
    """ERROR and MISSING nodes under ``root``, visiting only the branches that have one."""
    n, stack = 0, [root]
    while stack:
        node = stack.pop()
        if node.is_error or node.is_missing:
            n += 1
        stack.extend(c for c in node.children if c.has_error)
    return n


def _error_spans(root, limit: int = 40) -> list[tuple[int, int]]:
    """Where the ERROR and MISSING nodes are (1-based inclusive line pairs, in file order, at most ``limit``)."""
    out, stack = [], [root]
    while stack and len(out) < limit * 4:
        node = stack.pop()
        if node.is_error or node.is_missing:
            first = node.start_point[0] + 1
            out.append((first, max(first, node.end_point[0] + (1 if node.end_point[1] else 0))))
            continue
        stack.extend(c for c in node.children if c.has_error)
    return sorted(out)[:limit]


def _digest(node, data: bytes, skip: set[int] | None = None, mask: int | None = None) -> str:
    """A hash of the syntax tree under ``node`` (types and the text of the leaves), comments left out. Nested units whose node id is in ``skip`` count as a placeholder, and so does the
    node with id ``mask`` (the unit's own name)."""
    h = hashlib.blake2b(digest_size=10)
    stack: list = [node]
    while stack:
        n = stack.pop()
        if n is None:
            h.update(b")")
            continue
        t = n.type
        if "comment" in t:
            continue
        if mask is not None and n.id == mask:
            h.update(b"<name>")
            continue
        if skip is not None and n is not node and n.id in skip:
            h.update(b"<" + t.encode() + b">")
            continue
        kids = n.children
        if not kids:
            h.update(b"(" + t.encode() + b":" + data[n.start_byte:n.end_byte] + b")")
            continue
        h.update(b"(" + t.encode())
        stack.append(None)
        stack.extend(reversed(kids))
    return h.hexdigest()


class _Merkle:
    """Subtree hashes for the units of one parse. Every subtree is hashed once (types and the text of the leaves, comments left out) and shared by the three hashes of a unit, the whole
    unit, the unit without its nested units and the unit with its own name masked, and by the units nested in it. Only the nodes on the way to a nested unit or to the name are hashed again
    for those two variants."""

    def __init__(self, data: bytes, unit_nodes):
        self.data = data
        self.memo: dict[int, bytes] = {}
        self.placeholder = {n.id: hashlib.blake2b(b"P" + n.type.encode(), digest_size=10).digest() for n in unit_nodes}
        self.has_unit: set[int] = set()                    # the ancestors of unit nodes
        for n in unit_nodes:
            p = n.parent
            while p is not None and p.id not in self.has_unit:
                self.has_unit.add(p.id)
                p = p.parent

    def full(self, node) -> bytes:
        memo = self.memo
        got = memo.get(node.id)
        if got is not None:
            return got
        data = self.data
        stack: list = [(node, None)]
        while stack:
            n, live = stack.pop()
            if live is None:
                if n.id in memo:
                    continue
                kids = n.children
                if not kids:
                    memo[n.id] = hashlib.blake2b(b"L" + n.type.encode() + b":" + data[n.start_byte:n.end_byte], digest_size=10).digest()
                    continue
                live = [c for c in kids if "comment" not in c.type]
                stack.append((n, live))
                stack.extend((c, None) for c in live if c.id not in memo)
            else:
                memo[n.id] = hashlib.blake2b(b"N" + n.type.encode() + b"".join(memo[c.id] for c in live), digest_size=10).digest()
        return memo[node.id]

    def _variant(self, node, special: dict[int, bytes], dirty: set[int]) -> bytes:
        """The hash of ``node`` with the nodes of ``special`` replaced by their placeholder; only nodes in ``dirty`` (the ancestors of special nodes) are hashed again."""
        if node.id not in dirty:
            return self.full(node)
        local: dict[int, bytes] = {}
        stack: list = [(node, None)]
        while stack:
            n, live = stack.pop()
            if live is None:
                live = [c for c in n.children if "comment" not in c.type]
                stack.append((n, live))
                for c in live:
                    if c.id in special:
                        local[c.id] = special[c.id]
                    elif c.id in dirty:
                        stack.append((c, None))
            else:
                local[n.id] = hashlib.blake2b(b"N" + n.type.encode() + b"".join(local[c.id] if c.id in local else self.full(c) for c in live), digest_size=10).digest()
        return local[node.id]

    def whole(self, node) -> str:
        return self.full(node).hex()

    def without_nested(self, node) -> str:
        """The unit's own code: a unit nested in it counts as a placeholder."""
        return self._variant(node, self.placeholder, self.has_unit).hex()

    def name_masked(self, node, name_node) -> str:
        """The unit with its own name left out (two units with this hash are the same code under two names)."""
        if name_node is None:
            return self.whole(node)
        path: set[int] = set()
        p = name_node.parent
        hops = 0
        while p is not None and hops < 64:
            path.add(p.id)
            if p.id == node.id:
                break
            p = p.parent
            hops += 1
        return self._variant(node, {name_node.id: hashlib.blake2b(b"M", digest_size=10).digest()}, path).hex()


def _signature(fnode, data: bytes) -> tuple[str, int | None]:
    """The parameter list of a function node (whitespace collapsed) and how many parameters it has."""
    p = fnode.child_by_field_name("parameters")
    if p is None and fnode.type == "function_definition":              # C / C++: the parameters hang off the declarator
        d = fnode.child_by_field_name("declarator")
        while d is not None:
            if d.type == "function_declarator":
                p = d.child_by_field_name("parameters")
                break
            d = d.child_by_field_name("declarator")
    if p is None:
        p = fnode.child_by_field_name("parameter")                      # x => x
        if p is not None:
            return "(" + re.sub(r"\s+", " ", _text(p, data)).strip() + ")", 1
    if p is None:
        direct = [c for c in fnode.named_children if c.type == "parameter"]   # Swift lists them beside the name
        if direct:
            return "(" + ", ".join(re.sub(r"\s+", " ", _text(c, data)).strip() for c in direct) + ")", len(direct)
        for c in fnode.named_children:
            if "parameter" in c.type and "type_parameter" not in c.type:
                p = c
                break
    if p is None:
        return "", None
    text = re.sub(r"\s+", " ", _text(p, data)).strip()
    n = sum(1 for c in p.named_children if "comment" not in c.type)
    return text, 0 if text.replace(" ", "") == "(void)" else n


def _script_spans(src: str):
    """(attributes, start, end) of the body of every <script> block of a Vue / Svelte file; blocks inside HTML comments do not count, and an unclosed block ends the search. Linear in
    the size of ``src``: each closing tag is searched for once, from the end of its opening tag."""
    masked = _HTML_COMMENT.sub(lambda m: " " * len(m.group()), src)
    lower = masked.lower()
    pos = 0
    while True:
        m = _SCRIPT_OPEN.search(masked, pos)
        if m is None:
            return
        end = lower.find("</script", m.end())
        if end < 0:
            return
        yield m.group(1), m.end(), end
        close = masked.find(">", end)
        pos = close + 1 if close >= 0 else end + 8


def _script_blocks(src: str):
    """(attributes, body, offset of the body) of every <script> block of a Vue / Svelte file."""
    for attrs, start, end in _script_spans(src):
        yield attrs, src[start:end], start


_CONTINUES = re.compile(rb"^\s*(?:[})\]]|else\b|catch\b|finally\b|end\b|elif\b|except\b|[.?:,]|&&|\|\||\+|-\s)")


def _first_error(root):
    """The first ERROR node in document order that is not inside another (the outermost), or None."""
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "ERROR" or node.is_missing:
            return node
        if node.has_error:
            stack.extend(reversed(node.children))
    return None


def _recover(parser, data: bytes, tree, errors: int):
    """Error recovery by re-synchronising: tree-sitter reads a broken function and everything after it as one ERROR node. The first broken statement, up to the next line that is indented no
    more than it (a place where a new declaration can start), is replaced by spaces (offsets and line breaks kept) and the source is parsed again; the repaired parse is kept when it has fewer
    error nodes. At most ``MAX_RECOVERY`` regions. Returns (tree, errors, [(first byte, last byte) of each blanked region])."""
    regions: list[tuple[int, int]] = []
    cur = data
    while errors and len(regions) < MAX_RECOVERY:
        node = _first_error(tree.root_node)
        if node is None:
            break
        lines = cur.split(b"\n")
        r0 = node.start_point[0]
        indent0 = len(lines[r0]) - len(lines[r0].lstrip())
        r1 = len(lines)
        for l in range(r0 + 1, len(lines)):
            text = lines[l]
            if not text.strip():
                continue
            if len(text) - len(text.lstrip()) <= indent0 and not _CONTINUES.match(text):
                r1 = l
                break
        if r1 <= r0:
            r1 = r0 + 1
        start = sum(len(x) + 1 for x in lines[:r0])
        end = sum(len(x) + 1 for x in lines[:r1]) - 1 if r1 < len(lines) else len(cur)
        blanked = cur[:start] + re.sub(rb"[^\r\n]", b" ", cur[start:end]) + cur[end:]
        try:
            again = parser.parse(blanked)
        except MemoryError:
            raise
        except Exception:  # ignored: the parse so far is kept
            break
        n = _error_count(again.root_node)
        if n >= errors:
            break
        tree, errors, cur = again, n, blanked
        regions.append((start, end))
    return tree, errors, regions


def _extract(lang: str, spec_lang: str, src: str, out: _Units, seen: dict[str, int], *, line0: int = 0, byte0: int = 0, block: int = 0) -> None:
    """Add the units of ``src`` (one version, or one <script> block of it) to ``out``; ``line0`` and ``byte0`` place the text inside a larger file.

    A JavaScript file with errors is parsed again with the tsx grammar (Flow annotations and JSX are not JavaScript) and a C file with errors with the C++ grammar (a header with a class),
    and the parse with fewer errors is kept. Whatever the parse left broken is not dropped (a C macro would take real functions with it); ``errors`` on the result says how much was."""
    spec = _TS.get(spec_lang)
    if not spec:
        raise Unavailable(f"no_grammar:{lang}")
    grammar, funcs, containers = spec
    parser = _parser(grammar)
    if parser is None:
        raise Unavailable(f"grammar_unavailable:{lang}")
    data = src.encode("utf-8")
    try:
        tree = _parse_memo(grammar, parser, data)
    except MemoryError:
        raise
    except Exception:
        raise Unavailable("tree_sitter_error")
    errors = _error_count(tree.root_node)
    used = grammar
    if errors and grammar in ("javascript", "c"):
        alt_grammar = "tsx" if grammar == "javascript" else "cpp"
        retry_parser = _parser(alt_grammar)
        if retry_parser is not None:
            try:
                retry = _parse_memo(alt_grammar, retry_parser, data)
            except MemoryError:
                raise
            except Exception:  # ignored: the first parse is kept
                retry = None
            if retry is not None:
                n = _error_count(retry.root_node)
                if n < errors:
                    tree, errors, used = retry, n, alt_grammar
                    if grammar == "c":
                        grammar, funcs, containers = _TS["cpp"]
    first_errors = errors
    first_spans = _error_spans(tree.root_node) if errors else []
    blanked: list[tuple[int, int]] = []
    if errors and errors <= RECOVERY_MAX_ERRORS and len(data) <= 600_000:
        tree, errors, blanked = _recover(_parser(used) or parser, data, tree, errors)
    out.errors = max(out.errors, first_errors)
    out.recovered += len(blanked)
    out.grammar = used
    out.error_lines += [(a + line0, b + line0) for a, b in first_spans]
    js_like = grammar in ("javascript", "typescript", "tsx")
    span_nodes: dict[str, object] = {}
    name_nodes: dict[str, object] = {}

    def add(qual: str, span, fnode, parent: str | None, name: str, name_node) -> str:
        seen[qual] = seen.get(qual, 0) + 1
        key = qual if seen[qual] == 1 else f"{qual}#{seen[qual]}"
        end_row, end_col = span.end_point
        end_line = end_row if (end_col == 0 and end_row > span.start_point[0]) else end_row + 1     # a node that ends with its newline ends on the line before
        sig, arity = _signature(fnode, data)
        out._chars += span.end_byte - span.start_byte
        if out._chars > MAX_UNIT_CHARS:
            raise Unavailable("units_too_large")
        out[key] = (_text(span, data), [])
        out.info[key] = UnitInfo(key, qual, name, fnode.type, qual.rsplit(".", 1)[0] if "." in qual else "", parent, sig, arity,
                                 (span.start_point[0] + 1 + line0, end_line + line0), (span.start_byte + byte0, span.end_byte + byte0), "", "", "", block)
        span_nodes[key] = span
        name_nodes[key] = name_node
        if parent and parent in out:
            out[parent][1].append(key)
        return key

    def unit_of(node):
        """(name, node spanning the unit, the function node, the node holding the name) when ``node`` defines a function."""
        if node.type in funcs:
            span = node.parent if grammar == "python" and node.parent is not None and node.parent.type == "decorated_definition" else node
            return _name(node, data), span, node, _name_node(node)
        if js_like:
            if node.type == "variable_declarator":
                v = node.child_by_field_name("value")
                if v is not None and v.type in _JS_FUNC_VALUES:
                    span = node.parent if node.parent is not None and \
                        node.parent.named_child_count == 1 else node
                    return _name(node, data), span, v, node.child_by_field_name("name")
            if node.type == "assignment_expression":           # Foo.prototype.bar = function
                v = node.child_by_field_name("right")
                if v is not None and v.type in _JS_FUNC_VALUES:
                    left = node.child_by_field_name("left")
                    span = node.parent if node.parent is not None and \
                        node.parent.type == "expression_statement" else node
                    return (re.sub(r"\s+", "", _text(left, data))[:120] if left else None), span, v, left
            if node.type == "pair":                             # { key: function () {} }
                v = node.child_by_field_name("value")
                if v is not None and v.type in _JS_FUNC_VALUES:
                    k = node.child_by_field_name("key")
                    return (_text(k, data).strip("'\"")[:120] if k else None), node, v, k
        return None

    def walk(node, prefix: str, parent: str | None, depth: int = 0):
        if len(out) >= MAX_UNITS or depth > 200:
            return
        for child in node.named_children:
            if not child.named_child_count:
                continue                                    # a leaf is no function and holds none
            found = unit_of(child)
            name, span, fnode, name_node = found if found else (None, None, None, None)
            if name and not (errors and name.split(".")[-1] in _CONTROL_WORDS):
                if grammar == "go" and child.type == "method_declaration":
                    recv = child.child_by_field_name("receiver")
                    rtype = re.findall(r"[A-Za-z_]\w*", _text(recv, data))[-1:] if recv else []
                    name = f"{rtype[0]}.{name}" if rtype else name
                key = add(prefix + name, span, fnode, parent, name.rsplit(".", 1)[-1], name_node)
                walk(child, out.info[key].qual + ".", key, depth + 1)
            elif child.type in containers:
                cname = _name(child, data)
                walk(child, prefix + (cname + "." if cname else ""), parent, depth + 1)
            else:
                walk(child, prefix, parent, depth + 1)

    first = len(out)
    walk(tree.root_node, "", None)
    keys = list(out)[first:]
    merkle = _Merkle(data, [span_nodes[k] for k in keys])
    for k in keys:
        n = span_nodes[k]
        nn = name_nodes.get(k)
        if any(n.start_byte < hi and lo < n.end_byte for lo, hi in blanked):      # part of it was not read: its tree says nothing about that part, so its text does
            h = _blake(re.sub(r"\s+", " ", out[k][0]))
            out.info[k] = _with_hashes(out.info[k], h, h, h)
            continue
        out.info[k] = _with_hashes(out.info[k], merkle.whole(n), merkle.without_nested(n), merkle.name_masked(n, nn))


def _with_hashes(info: UnitInfo, shash: str, ohash: str, nhash: str) -> UnitInfo:
    return UnitInfo(info.key, info.qual, info.name, info.kind, info.scope, info.parent, info.signature, info.arity, info.lines, info.bytes, shash, ohash, nhash, info.block)


def _too_large(text: str) -> int | None:
    """The size in bytes of ``text`` when it is over ``MAX_SOURCE_BYTES``, else None. A string with more characters than the cap is over it; one with at most a quarter of the cap is
    under it; only the ones between are encoded to be measured."""
    n = len(text)
    if n > MAX_SOURCE_BYTES:
        return n
    if n * 4 <= MAX_SOURCE_BYTES:
        return None
    size = len(text.encode("utf-8", "surrogatepass"))
    return size if size > MAX_SOURCE_BYTES else None


def _build_units(lang: str, path: str, src: str, tree: bool = False) -> _Units:
    """The units of one version of a file, parsed now (the cache is not asked); raises ``Unavailable`` with the reason. ``tree`` reads a Python file with the tree-sitter grammar even
    when ``ast`` would accept it (see ``parse_pair``)."""
    big = _too_large(src)
    if big is not None:
        raise Unavailable(f"source_too_large:{big}")
    if lang == "python":
        try:
            if tree:
                raise SyntaxError("read with the tree-sitter grammar")
            return _python_units(src)
        except (SyntaxError, ValueError, RecursionError):
            out = _Units()
            try:
                _extract("python", "python", src, out, {})          # Python 2, merge conflict markers, a truncated file: read what the tree-sitter grammar can
            except Unavailable:
                raise Unavailable("python_syntax_error")
            if out.errors > RECOVERY_MAX_ERRORS and not len(out):
                raise Unavailable("python_syntax_error")             # nothing readable and mostly noise
            return out
    if lang in ("vue", "svelte"):
        out, seen = _Units(), {}
        for i, (attrs, body, offset) in enumerate(_script_blocks(src)):
            sub = "typescript" if re.search(r"lang\s*=\s*[\"']ts", attrs) else "javascript"
            _extract(sub, sub, body, out, seen, line0=src.count("\n", 0, offset), byte0=len(src[:offset].encode("utf-8")), block=i)
        return out
    spec_lang = "tsx" if lang == "typescript" and path.lower().endswith(".tsx") else lang
    out = _Units()
    _extract(lang, spec_lang, src, out, {})
    return out


_SUFFIX = re.compile(r"#\d+$")


def _base(qual: str) -> str:
    """A qualified name without the ``#n`` that tells the n-th definition of a repeated name (a private member ``#x`` has no digits and keeps its ``#``)."""
    return _SUFFIX.sub("", qual)


# ── caches: parse results addressed by content, language and parser versions ──

class _Entry:
    __slots__ = ("value", "size")

    def __init__(self, value, size: int):
        self.value, self.size = value, size


class _LRU:
    """A least-recently-used cache bounded by the size each entry is given and by the number of entries (the limits are module constants read at the time, so a test can change them)."""

    def __init__(self, bytes_limit: str, entries_limit: str):
        self._data: "collections.OrderedDict[tuple, _Entry]" = collections.OrderedDict()
        self._lock = threading.Lock()
        self._limits = (bytes_limit, entries_limit)
        self.hits = self.misses = self.bytes = 0

    def get(self, key):
        with self._lock:
            hit = self._data.get(key)
            if hit is None:
                self.misses += 1
                return None
            self._data.move_to_end(key)
            self.hits += 1
            return hit

    def put(self, key, value, size: int) -> None:
        g = globals()
        with self._lock:
            old = self._data.pop(key, None)
            if old is not None:
                self.bytes -= old.size
            self._data[key] = _Entry(value, size)
            self.bytes += size
            while self._data and (self.bytes > g[self._limits[0]] or len(self._data) > g[self._limits[1]]):
                _, gone = self._data.popitem(last=False)
                self.bytes -= gone.size

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self.hits = self.misses = self.bytes = 0

    def items(self):
        with self._lock:
            return list(self._data.items())

    def __iter__(self):
        return iter([k for k, _ in self.items()])

    def __getitem__(self, key):
        return self._data[key]

    def __len__(self):
        return len(self._data)


STREAM_BYTES = 16 * 2 ** 20
STREAM_ENTRIES = 4096
_cache = _LRU("CACHE_BYTES", "CACHE_ENTRIES")                 # parse_units results
_streams = _LRU("STREAM_BYTES", "STREAM_ENTRIES")             # the syntax-tree hash of a piece of code
_REMEMBERED = ("python_syntax_error", "source_too_large", "units_too_large")      # failures that follow from the content alone
_RETRY_GRAMMAR = {"javascript": "tsx", "typescript": "tsx", "vue": "tsx", "svelte": "tsx", "c": "cpp"}      # the grammar a file with parse errors is read with again


def _grammar_availability(lang: str, variant: str = "") -> tuple:
    """Extraction and no-op comparison depend on runtime grammar availability."""
    if lang in ("vue", "svelte"):
        grammars = {"javascript", "typescript", "tsx"}
    else:
        spec = _TS.get("tsx" if lang == "typescript" and variant == "tsx" else lang)
        grammars = {spec[0]} if spec else set()
        if lang in _RETRY_GRAMMAR:
            grammars.add(_RETRY_GRAMMAR[lang])
    # Hunk and residual comparisons use the language's stream grammar even when
    # units were parsed with a file-specific variant, notably TypeScript/TSX.
    stream = _STREAM_GRAMMAR.get(lang)
    if stream:
        grammars.add(stream)
        if stream in _RETRY_GRAMMAR:
            grammars.add(_RETRY_GRAMMAR[stream])
    return tuple((name, _parser(name) is not None) for name in sorted(grammars))


def _content_key(kind: str, lang: str, variant: str, code: str) -> tuple:
    """Key content, language/variant, parser versions and all grammar availability."""
    return (kind, VERSION, parser_fingerprint(), lang, variant, _grammar_availability(lang, variant), hashlib.blake2b(code.encode("utf-8", "surrogatepass"), digest_size=16).digest())


def clear_cache() -> None:
    _cache.clear()
    _streams.clear()
    _local.trees = collections.OrderedDict()                 # this thread's recent parses


def cache_info() -> dict:
    return {"hits": _cache.hits + _streams.hits, "misses": _cache.misses + _streams.misses, "bytes": _cache.bytes + _streams.bytes,
            "entries": len(_cache) + len(_streams)}


def verify_cache() -> list[str]:
    """Parse again what the caches hold and say which entries no longer equal a fresh result (an empty list: consistent). Meant for tests and for a suspicious run, not for the hot path."""
    problems = []
    for key, entry in _cache.items():
        value = entry.value
        if isinstance(value, Unavailable):
            continue
        lang, path, src, tree = value._source
        again = _build_units(lang, path, src, tree)
        if dict(again) != dict(value) or again.info != value.info or again.errors != value.errors:
            problems.append(f"units:{lang}:{key[-1].hex()[:12]}")
    for key, entry in _streams.items():
        value, (lang, code) = entry.value
        if _compute_stream(lang, code) != value:
            problems.append(f"stream:{lang}:{key[-1].hex()[:12]}")
    return problems


def parse_units(lang: str, path: str, src: str, tree: bool = False) -> _Units:
    """The units of one version of a file with their spans, signatures and hashes; raises ``Unavailable`` with the reason. The result is shared: do not modify it. ``tree`` is for
    Python only: read the file with the tree-sitter grammar whether or not ``ast`` accepts it."""
    big = _too_large(src)
    if big is not None:                       # before the source is hashed for the cache
        raise Unavailable(f"source_too_large:{big}")
    tree = tree and lang == "python"
    key = _content_key("units", lang, "tsx" if lang == "typescript" and path.lower().endswith(".tsx") else ("tree" if tree else ""), src)
    hit = _cache.get(key)
    if hit is not None:
        if isinstance(hit.value, Unavailable):
            raise hit.value
        return hit.value
    try:
        units = _build_units(lang, path, src, tree)
    except Unavailable as e:
        if str(e).startswith(_REMEMBERED):
            _cache.put(key, e, 200)
        raise
    units._source = (lang, path, src, tree)
    _cache.put(key, units, 2 * len(src) + 1000)
    return units


def parse_pair(lang: str, path: str, before: str, after: str) -> tuple[_Units, _Units]:
    """The units of both versions of a file, read so that their hashes compare. A Python file the standard ``ast`` accepts and one it rejects (a syntax error, Python 2) are read by
    different engines whose hashes are unrelated; when the two versions differ in that, both are read with the tree-sitter grammar. Raises ``Unavailable``."""
    b, a = parse_units(lang, path, before), parse_units(lang, path, after)
    if lang == "python" and b.engine != a.engine:
        b, a = parse_units(lang, path, before, True), parse_units(lang, path, after, True)
    return b, a


def _units_strict(lang: str, path: str, src: str) -> _Units:
    """Units of one version, or ``Unavailable`` with the reason."""
    return parse_units(lang, path, src)


def _units(lang: str, path: str, src: str) -> dict[str, tuple[str, list[str]]] | None:
    try:
        return _units_strict(lang, path, src)
    except Unavailable:
        return None


def _units_pair(lang: str, path: str, before: str, after: str):
    """``parse_pair``, or None when a version cannot be parsed."""
    try:
        return parse_pair(lang, path, before, after)
    except Unavailable:
        return None


# ── whether two versions differ only in comments or formatting: the syntax tree decides ──

# grammars for the syntax-tree comparison: the languages that have units, and a few that only get compared
_STREAM_GRAMMAR = {**{lang: spec[0] for lang, spec in _TS.items() if lang != "tsx"}, "solidity": "solidity", "shell": "bash", "elixir": "elixir", "html": "html",
                   "objective-c": "objc", "dart": "dart", "lua": "lua", "erlang": "erlang", "perl": "perl", "r": "r", "yaml": "yaml", "sql": "sql", "groovy": "groovy"}
_NL = re.compile(r"\r\n|\r")


def _tree_digest(lang: str, code: str) -> tuple[str, str] | None:
    """(grammar, hash) of the syntax tree of ``code`` without comments, or None when the language has no parser. A source with errors is read again with the alternative grammar when that
    gives fewer errors (JavaScript or TypeScript as tsx, C as C++); two sources are only equal when the same grammar read both."""
    primary = _STREAM_GRAMMAR.get(lang)
    parser = _parser(primary) if primary else None
    if parser is None:
        return None
    data = code.encode("utf-8", "replace")
    try:
        tree = parser.parse(data)
    except MemoryError:
        raise
    except Exception:  # ignored: no tree, the caller compares the text
        return None
    errors, used = _error_count(tree.root_node), primary
    alt = _RETRY_GRAMMAR.get(primary)
    if errors and alt:
        retry_parser = _parser(alt)
        if retry_parser is not None:
            try:
                retry = retry_parser.parse(data)
                n = _error_count(retry.root_node)
                if n < errors:
                    tree, errors, used = retry, n, alt
            except MemoryError:
                raise
            except Exception:  # ignored: the first parse is kept
                pass
    return used, _digest(tree.root_node, data)


def _template_text(src: str, bodies: list[tuple[int, int]]) -> str:
    """The part of a Vue / Svelte file outside its scripts, without HTML comments, runs of whitespace as one space (whitespace that appears or disappears is a change)."""
    parts, pos = [], 0
    for a, b in bodies:
        parts.append(src[pos:a])
        pos = b
    parts.append(src[pos:])
    return re.sub(r"\s+", " ", _HTML_COMMENT.sub("", "\x00".join(parts))).strip()


def _compute_stream(lang: str, code: str) -> tuple | None:
    if lang == "python":
        try:
            tree = parse_python(code)
            return "ast", hashlib.blake2b(_py_dump(tree, tree, False).encode("utf-8"), digest_size=10).hexdigest()
        except (SyntaxError, ValueError, RecursionError):
            return None
    if lang in ("vue", "svelte"):
        spans, parts = [], []
        for attrs, start, end in _script_spans(code):
            sub = "typescript" if re.search(r"lang\s*=\s*[\"']ts", attrs) else "javascript"
            digest = _tree_digest(sub, code[start:end])
            if digest is None:
                return None
            spans.append((start, end))
            parts.append(digest)
        return "sfc", tuple(parts), _template_text(code, spans)
    return _tree_digest(lang, code)


def _stream(lang: str | None, code: str | None) -> tuple | None:
    """The comparable form of the syntax tree of ``code`` (cached by content), or None when there is no parser for the language or the text is not a program of it."""
    if not lang:
        return None
    code = _NL.sub("\n", code or "")
    if _too_large(code) is not None:
        return None                           # no tree for a source over the cap: the comparison falls back to the lexical one
    key = _content_key("stream", lang, "", code)
    hit = _streams.get(key)
    if hit is not None:
        return hit.value[0]
    value = _compute_stream(lang, code)
    _streams.put(key, (value, (lang, code)), 2 * len(code) + 500)
    return value


def is_noop(lang: str | None, before: str | None, after: str | None) -> bool:
    """True when two versions differ only in comments or formatting.

    Where the language has a parser the answer comes from the syntax trees: equal trees (types and leaf text, comments left out) mean nothing a program can observe changed, and a lossy
    string normaliser can no longer make ``a + ++b`` and ``a+++b`` equal. Python compares ``ast`` trees (so quotes and redundant parentheses are formatting). Line endings are not a change.
    Without a parser, or for text that is not a program (a hunk), the comparison is lexical and errs towards "changed"."""
    if before == after:
        return True
    a, b = _stream(lang, before), _stream(lang, after)
    if a is not None and b is not None:
        return a == b
    return canonical(lang, before) == canonical(lang, after)


# ── matching the units of two versions ──────────────────────────────────────

_IDENT = re.compile(r"[A-Za-z_$][A-Za-z_0-9$]*|\d+")
_HASH_COMMENT = re.compile(r"#[^\n]*")
_SLASH_COMMENT = re.compile(r"//[^\n]*|/\*(?:[^*]|\*(?!/))*(?:\*/|\Z)")        # linear: an unclosed block comment runs to the end and is one match
_STOP = frozenset("def function fn func fun sub return public private protected static final async void self this var let const".split())


def _content_tokens(lang: str | None, info: UnitInfo, text: str) -> list[str]:
    """The identifiers and literals of a unit's body, for comparing bodies. Left out: operators and brackets (they say little about which function it is), the declaration keywords every
    function has, the parameters (they belong to the signature) and the declaration of the unit's own name; later uses of its own name (a recursive call) are masked, so a rename is not a change."""
    if lang == "python" or lang in _HASH:
        text = _HASH_COMMENT.sub(" ", text)
    elif lang in _C_LIKE:
        text = _SLASH_COMMENT.sub(" ", text)
    own = {t for t in _IDENT.findall(info.signature)}
    out, declared = [], False
    for t in _IDENT.findall(text):
        if t == info.name:
            if declared:
                out.append("\x00N")
            declared = True
        elif t not in _STOP and t not in own:
            out.append(t)
    return out


def match_units(lang: str, b: _Units, a: _Units) -> splitmatch.MatchResult:
    """Which unit of ``b`` (before) is which unit of ``a`` (after): matches with a kind, a confidence and the signals behind it, links for the non-bijective cases (split, merge, ambiguous),
    and the units left without a counterpart. See ``splitmatch``."""
    return splitmatch.match_units(b.info, a.info, lambda k: _content_tokens(lang, b.info[k], b[k][0]), lambda k: _content_tokens(lang, a.info[k], a[k][0]))


_SAME_NAME = ("same", "same_repeated", "positional")


def _changed_matches(b: _Units, a: _Units, matches) -> list[tuple]:
    """(key after, code before, code after, key before or None, match) of the matches whose code changed. Two units are the same code when their syntax trees (comments left out) hash alike;
    a unit whose own code (nested units left out) hashes alike changed only inside a nested function, and the nested one is the pair. A renamed or moved unit is compared with its name masked."""
    out = []
    for m in matches:
        bi, ai = b.info[m.before], a.info[m.after]
        renamed = m.kind in ("renamed", "moved")
        if (bi.nhash == ai.nhash) if renamed else (bi.shash == ai.shash):
            continue
        bnest, anest = b[m.before][1], a[m.after][1]
        if (bnest or anest) and not renamed and bi.ohash == ai.ohash:
            continue
        out.append((m.after, b[m.before][0], a[m.after][0], m.before if m.before != m.after else None, m))
    return out


def _changed(lang, b, a, result: splitmatch.MatchResult | None = None):
    """(key after, code before, code after, key before or None) of the units present in both versions under one name whose code changed."""
    result = result or match_units(lang, b, a)
    return [t[:4] for t in _changed_matches(b, a, [m for m in result.matches if m.kind in _SAME_NAME])]


def _renamed(lang, b, a, result: splitmatch.MatchResult | None = None):
    """(new, before, after, old): units that changed their name or scope and whose code changed too. A pure rename is not a security change and gives no pair."""
    result = result or match_units(lang, b, a)
    return [t[:4] for t in _changed_matches(b, a, [m for m in result.matches if m.kind not in _SAME_NAME])]


def changed_functions(lang: str, path: str, before: str, after: str) -> list[tuple[str, str, str]] | None:
    """(qualname, before, after) of the functions present in both versions whose
    code changed (not only comments or formatting); None when the language cannot
    be parsed."""
    pair = _units_pair(lang, path, before, after)
    if pair is None:
        return None
    b, a = pair
    return [(q, x, y) for q, x, y, _ in _changed(lang, b, a)]


def renamed_functions(lang: str, path: str, before: str, after: str) -> list[tuple[str, str, str, str]] | None:
    pair = _units_pair(lang, path, before, after)
    if pair is None:
        return None
    return _renamed(lang, *pair)


def unchanged_functions(lang: str, path: str, before: str, after: str,
                        min_lines: int = 3) -> list[tuple[str, str]] | None:
    """(key, code) of the functions the fix left untouched: matched one to one with a function of the other version under the same name, with identical code.

    "Untouched" is all this says. It is **not** evidence that the function is secure: the file may hold a vulnerability the fix did not reach, so the detection set leaves these
    functions out unless they are asked for as weak, unverified negatives. Matching is by ``match_units`` (overloads that changed places are paired by signature and body, not by
    position), a pairing that is only a guess (``positional``) is not used, a function that overlaps a parse error in either version is left out, and so is a nested function (its text
    is part of the function around it). None when the language cannot be parsed."""
    pair = _units_pair(lang, path, before, after)
    if pair is None:
        return None
    b, a = pair
    result = match_units(lang, b, a)

    def broken(units, info) -> bool:
        return any(lo <= info.lines[1] and info.lines[0] <= hi for lo, hi in units.error_lines)

    out = []
    for m in result.matches:
        if m.kind not in ("same", "same_repeated"):
            continue
        bi, ai = b.info[m.before], a.info[m.after]
        if ai.parent is not None or bi.parent is not None or broken(b, bi) or broken(a, ai):
            continue
        if bi.shash != ai.shash or util.norm_code(b[m.before][0]) != util.norm_code(a[m.after][0]):
            continue
        code = a[m.after][0]
        if len(util.norm_code(code).splitlines()) >= min_lines:
            out.append((m.after, code))
    return sorted(out)


# ── a line differ that stays cheap on large files ────────────────────────────

LINE_DIFF_CELLS = 4_000_000        # before x after line counts up to this are diffed by difflib (exact, quadratic in the worst case); beyond it the differ anchors on unique lines


SMALL_DIFF_CELLS = 40_000          # up to this (200 x 200 lines) difflib is fast whatever the content
ALIKE = 0.3                        # the share of lines two versions must have in common for difflib's exact search to be worth its cost


def _alike(bl, al) -> bool:
    """Whether enough lines occur on both sides for an exact diff to pay: difflib is quadratic exactly when the two texts have little in common."""
    sb, sa = set(bl), set(al)
    return len(sb & sa) >= ALIKE * min(len(sb), len(sa))


def _anchors(bl, al, x1: int, x2: int, y1: int, y2: int) -> list[tuple[int, int]]:
    """Pairs (line in before, line in after) of lines that occur exactly once in each range, in the longest order-preserving subsequence (patience diff)."""
    cb = collections.Counter(bl[x1:x2])
    ca = collections.Counter(al[y1:y2])
    pos_a = {al[j]: j for j in range(y1, y2) if ca[al[j]] == 1}
    cand = [(i, pos_a[bl[i]]) for i in range(x1, x2) if cb[bl[i]] == 1 and bl[i] in pos_a]
    if not cand:
        return []
    tails: list[int] = []            # patience sorting on the after-position, remembering predecessors
    tail_idx: list[int] = []
    prev = [-1] * len(cand)
    for k, (_, j) in enumerate(cand):
        t = bisect.bisect_left(tails, j)
        if t == len(tails):
            tails.append(j)
            tail_idx.append(k)
        else:
            tails[t] = j
            tail_idx[t] = k
        prev[k] = tail_idx[t - 1] if t else -1
    out, k = [], tail_idx[-1]
    while k != -1:
        out.append(cand[k])
        k = prev[k]
    return out[::-1]


def _tidy(ops: list[tuple]) -> list[tuple]:
    """Merge neighbouring blocks of the same kind and neighbouring changes into one."""
    out: list[list] = []
    for tag, i1, i2, j1, j2 in ops:
        if i1 == i2 and j1 == j2:
            continue
        if out and out[-1][0] == tag == "equal":
            out[-1][2], out[-1][4] = i2, j2
        elif out and out[-1][0] != "equal" and tag != "equal":
            out[-1][2], out[-1][4] = i2, j2
            out[-1][0] = "replace" if out[-1][2] > out[-1][1] and out[-1][4] > out[-1][3] else ("delete" if out[-1][2] > out[-1][1] else "insert")
        else:
            out.append([tag, i1, i2, j1, j2])
    return [tuple(o) for o in out]


def line_ops(bl: list[str], al: list[str]) -> list[tuple[str, int, int, int, int]]:
    """Opcodes (``equal`` / ``replace`` / ``delete`` / ``insert``, with index ranges) that turn the lines ``bl`` into ``al``, like ``SequenceMatcher.get_opcodes()``.

    For inputs of ordinary size this is ``difflib`` itself. For large ones the common start and end are cut off, then lines that occur once on each side anchor the alignment (patience
    diff) and only the stretches between anchors are diffed, so two large unrelated files cost a scan, not a quadratic search."""
    n, m = len(bl), len(al)
    if n * m <= LINE_DIFF_CELLS:
        if n * m <= SMALL_DIFF_CELLS or _alike(bl, al):
            return difflib.SequenceMatcher(None, bl, al, autojunk=False).get_opcodes()
    i = 0
    while i < n and i < m and bl[i] == al[i]:
        i += 1
    j = 0
    while j < n - i and j < m - i and bl[n - 1 - j] == al[m - 1 - j]:
        j += 1
    result: list[tuple] = []
    if i:
        result.append(("equal", 0, i, 0, i))
    work: list[tuple] = [("gap", i, n - j, i, m - j)]
    while work:
        kind, x1, x2, y1, y2 = work.pop()
        if kind == "eq":
            result.append(("equal", x1, x2, y1, y2))
            continue
        if x1 == x2 and y1 == y2:
            continue
        if x1 == x2:
            result.append(("insert", x1, x1, y1, y2))
            continue
        if y1 == y2:
            result.append(("delete", x1, x2, y1, y1))
            continue
        cells = (x2 - x1) * (y2 - y1)
        if cells <= SMALL_DIFF_CELLS or (cells <= LINE_DIFF_CELLS and _alike(bl[x1:x2], al[y1:y2])):
            for tag, a1, a2, b1, b2 in difflib.SequenceMatcher(None, bl[x1:x2], al[y1:y2], autojunk=False).get_opcodes():
                result.append((tag, a1 + x1, a2 + x1, b1 + y1, b2 + y1))
            continue
        anchors = _anchors(bl, al, x1, x2, y1, y2)
        if not anchors:
            result.append(("replace", x1, x2, y1, y2))
            continue
        pieces, px, py = [], x1, y1
        for bx, ay in anchors:
            pieces += [("gap", px, bx, py, ay), ("eq", bx, bx + 1, ay, ay + 1)]
            px, py = bx + 1, ay + 1
        pieces.append(("gap", px, x2, py, y2))
        work.extend(reversed(pieces))
    if j:
        result.append(("equal", n - j, n, m - j, m))
    return _tidy(result)


# ── hunks ────────────────────────────────────────────────────────────────────

_OPEN = re.compile(r"^\s*(/\*|\"\"\"|\'\'\')")      # starts a block comment / docstring


def _trim(bl: list[str], lo: int, hi: int, first: int, last: int) -> tuple[int, int]:
    """Shrink the context [lo, first) and [last, hi) of a region (same text on both
    sides): no blank lines at the edges, no block comment cut off at the end, and
    no tail of a block comment at the start."""
    for i in range(lo, first):                       # starts inside a comment: skip to its end
        t = bl[i].strip()                            # (a comment line, not e.g. '**/*.js')
        if (t.startswith("*/") or (t.startswith("*") and t.endswith("*/"))) \
                and "/*" not in "".join(bl[lo:i]):
            lo = i + 1
    for i in range(last, hi):                        # a block comment at the end: cut before it
        if not _OPEN.match(bl[i]):
            continue
        token = "*/" if "/*" in bl[i] else bl[i].strip()[:3]
        rest = bl[i].split(token, 1)[1] if token != "*/" else bl[i].split("/*", 1)[1]
        close = i if token in rest else next((k for k in range(i + 1, hi) if token in bl[k]), None)
        # unclosed in the region, or nothing but blank lines after it: it belongs to
        # the code that follows the hunk
        if close is None or all(not x.strip() for x in bl[close + 1:hi]):
            hi = i
        break
    while lo < first and not bl[lo].strip():
        lo += 1
    while hi > last and not bl[hi - 1].strip():
        hi -= 1
    return lo, hi


def _raw_lines(src: str) -> list[str]:
    """The lines of a source split at ``\\n`` only (the unit spans count lines that way), line endings other than ``\\n`` kept on the line, no empty last line for a final newline."""
    lines = src.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _window(rb: list[str], ra: list[str], i1: int, i2: int, j1: int, j2: int, context: int, blocked_b=None, blocked_a=None) -> tuple[int, int, int, int]:
    """(lo_b, hi_b, lo_a, hi_a): the lines of both versions around the changed region ``[i1:i2]`` / ``[j1:j2]`` with up to ``context`` lines of unchanged context on both sides (the same
    lines in both versions), trimmed of blank edge lines and cut-off block comments. Context never takes in a line that ``blocked_b`` / ``blocked_a`` (line indexes inside a function pair)
    contain, and when blocked lines are given it only takes in lines that really are equal in both versions (the region may start in the middle of a larger change). The region's own
    lines are always inside."""
    lo_b, hi_b = max(0, i1 - context), min(len(rb), i2 + context)
    lead, tail = i1 - lo_b, hi_b - i2
    lead, tail = min(lead, j1), min(tail, len(ra) - j2)          # same context both sides
    if blocked_b is not None:
        while lead and [l.rstrip("\r") for l in rb[i1 - lead:i1]] != [l.rstrip("\r") for l in ra[j1 - lead:j1]]:
            lead -= 1
        while tail and [l.rstrip("\r") for l in rb[i2:i2 + tail]] != [l.rstrip("\r") for l in ra[j2:j2 + tail]]:
            tail -= 1
    if blocked_b is not None:
        while lead and (any(i in blocked_b for i in range(i1 - lead, i1)) or any(j in blocked_a for j in range(j1 - lead, j1))):
            lead -= 1
        while tail and (any(i in blocked_b for i in range(i2, i2 + tail)) or any(j in blocked_a for j in range(j2, j2 + tail))):
            tail -= 1
    lo_b, hi_b = i1 - lead, i2 + tail
    for lo_b2, hi_b2 in (_trim(rb, lo_b, hi_b, i1, i2), (lo_b, hi_b)):
        lo_a, hi_a = j1 - (i1 - lo_b2), j2 + (hi_b2 - i2)
        if "\n".join(rb[lo_b2:hi_b2]).strip() and "\n".join(ra[lo_a:hi_a]).strip():
            break                                # trimming must leave code on both sides
    return lo_b2, hi_b2, lo_a, hi_a


def _ops(rb: list[str], ra: list[str]) -> list[tuple]:
    """The non-equal opcodes between two versions; a line ending (``\\r``) is not a difference."""
    return [op for op in line_ops([l.rstrip("\r") for l in rb], [l.rstrip("\r") for l in ra]) if op[0] != "equal"]


def _hunk_records(rb: list[str], ra: list[str], ops: list[tuple], lang: str | None, context: int = CONTEXT) -> list[dict]:
    """One record per changed region with its context (regions whose context overlaps are merged): the window, its two texts, and whether it makes a pair (code on both sides and not
    only comments or formatting)."""
    regions: list[list[int]] = []        # [i1, i2, j1, j2] of the changes, merged
    for _, i1, i2, j1, j2 in ops:
        if regions and i1 - regions[-1][1] <= 2 * context:
            regions[-1][1], regions[-1][3] = i2, j2
        else:
            regions.append([i1, i2, j1, j2])
    out = []
    for i1, i2, j1, j2 in regions:
        lo_b, hi_b, lo_a, hi_a = _window(rb, ra, i1, i2, j1, j2, context)
        b, a = "\n".join(rb[lo_b:hi_b]), "\n".join(ra[lo_a:hi_a])
        # a pure insertion / deletion with no code around it is not a vulnerable -> fixed pair
        both = bool(b.strip() and a.strip())
        out.append({"win": (lo_b, hi_b, lo_a, hi_a), "ops": (i1, i2, j1, j2), "before": b, "after": a, "noop": both and is_noop(lang, b, a), "usable": both and not is_noop(lang, b, a)})
    return out


def hunks(before: str, after: str, context: int = CONTEXT, lang: str | None = None) -> list[tuple[str, str, str]]:
    """(span, before, after) per changed region with up to ``context`` lines around
    it; regions whose context overlaps are merged, and the context is trimmed of
    blank edge lines and cut-off block comments. ``span`` is "start-end" (1-based
    lines of the vulnerable version). A region that only differs in comments or
    formatting (for a language that is known) is not a change."""
    rb, ra = _raw_lines(before), _raw_lines(after)
    return [(f"{r['win'][0] + 1}-{max(r['win'][1], r['win'][0] + 1)}", r["before"], r["after"]) for r in _hunk_records(rb, ra, _ops(rb, ra), lang, context) if r["usable"]]


# ── what became of every changed line ────────────────────────────────────────

SUBSTANTIVE = frozenset({"constant", "class_member", "class_header", "type_definition", "annotation", "template", "export", "module_code", "unparsed", "unsplit"})
BUCKETS = ("in_pairs", "unpaired_units", "moved_units", "residual_emitted", "residual_recorded", "comment_or_blank")


class Analysis:
    """The outcome of splitting one changed file.

    ``pairs`` are the sub-file pairs (what ``split_with_status`` returns). Beside them, nothing that changed is left unsaid: ``unpaired`` are functions added or removed, ``residual`` the
    changes outside every function (``kind``: import, constant, class_member, class_header, type_definition, annotation, template, export, module_code, unparsed, unsplit; ``emitted`` says
    whether a hunk pair carries it), ``noop_regions`` comments and moved or renamed functions, ``links`` the matches that are not one to one. ``coverage`` is the ledger: every changed line
    is in exactly one of its buckets (``BUCKETS``) and ``unaccounted`` is what is in none (it should be 0)."""

    def __init__(self, pairs: list[dict], status: str, reason: str | None):
        self.pairs, self.status, self.reason = pairs, status, reason
        self.unpaired: list[dict] = []
        self.residual: list[dict] = []
        self.noop_regions: list[dict] = []
        self.links: list[dict] = []
        self.coverage: dict = {}
        self.parser: dict = {"fingerprint": parser_fingerprint()}
        self.matches: list = []

    def to_dict(self) -> dict:
        return {"status": self.status, "reason": self.reason, "unpaired": self.unpaired, "residual": self.residual, "noop_regions": self.noop_regions, "links": self.links,
                "coverage": self.coverage, "parser": self.parser}


def _owners(infos: dict[str, UnitInfo], lines: list[int]) -> dict[int, str]:
    """For each line, the innermost unit that contains it (units nest or are apart): a sweep over the units and the lines in order."""
    order = sorted(infos.values(), key=lambda u: (u.lines[0], -u.lines[1]))
    out: dict[int, str] = {}
    stack: list[UnitInfo] = []
    i = 0
    for line in sorted(lines):
        while i < len(order) and order[i].lines[0] <= line:
            stack.append(order[i])
            i += 1
        k = len(stack) - 1
        while k >= 0 and stack[k].lines[1] < line:           # units that ended before this line
            k -= 1
        if k >= 0:
            out[line] = stack[k].key
    return out


_REQUIRE = re.compile(r"\s*(?:export\s+)?(?:const|let|var)\b[^=]*=\s*(?:await\s+)?require\s*\(")
_IMPORT_WORDS = ("import", "use_declaration", "using", "include", "require", "namespace_use", "package_clause", "extern_crate", "preproc_include", "load_statement")
_TYPE_WORDS = ("type_alias", "type_declaration", "type_definition", "interface", "struct", "enum", "trait", "typedef", "type_spec", "union", "protocol")
_DECL_WORDS = ("const", "static", "let", "var", "lexical", "variable", "assignment", "declaration", "define", "preproc_def", "global", "field", "property", "constant", "val", "short_var")
_ANNOTATION_WORDS = ("decorator", "annotation", "attribute", "modifier")


def _kind_of(node_type: str) -> str:
    """A label for what kind of statement a syntax node type is (describes a change outside every function; it decides nothing)."""
    t = node_type.lower()
    if "comment" in t:
        return "comment"
    if any(w in t for w in _IMPORT_WORDS):
        return "import"
    if any(w in t for w in _ANNOTATION_WORDS):
        return "annotation"
    if any(w in t for w in _TYPE_WORDS):
        return "type_definition"
    if "export" in t:
        return "export"
    if any(w in t for w in _DECL_WORDS):
        return "constant"
    return "module_code"


def _python_line_kinds(src: str, lines: list[int]) -> dict[int, str]:
    tree = parse_python(src)
    spans: list[tuple[int, int, str]] = []

    def visit(body, in_class: bool):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            first = min([node.lineno, *(d.lineno for d in getattr(node, "decorator_list", []))])
            if isinstance(node, ast.ClassDef):
                spans.append((first, node.body[0].lineno - 1 if node.body else node.end_lineno, "class_header"))
                visit(node.body, True)
                continue
            if isinstance(node, (ast.If, ast.Try, ast.With, ast.AsyncWith, ast.For, ast.AsyncFor, ast.While)):
                spans.append((first, node.body[0].lineno - 1 if node.body else node.end_lineno, "class_member" if in_class else "module_code"))
                for block in _blocks(node):
                    visit(block, in_class)
                continue
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                kind = "import"
            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                kind = "comment"                                                   # a docstring
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                kind = "class_member" if in_class else "constant"
            else:
                kind = "class_member" if in_class else "module_code"
            spans.append((first, node.end_lineno, kind))
    visit(tree.body, False)
    spans.sort(key=lambda s: (s[0], -s[1]))
    raw = _raw_lines(src)
    out: dict[int, str] = {}
    for line in lines:
        text = raw[line - 1] if 0 < line <= len(raw) else ""
        if not text.strip():
            out[line] = "blank"
        elif text.lstrip().startswith("#"):
            out[line] = "comment"
        else:
            hit = [s for s in spans if s[0] <= line <= s[1]]
            out[line] = hit[-1][2] if hit else "module_code"
    return out


def _tree_line_kinds(grammar_lang: str, src: str, lines: list[int], line0: int = 0) -> dict[int, str]:
    """Kinds of lines outside every function, from the smallest syntax node at the line's first character and its ancestors. ``grammar_lang`` is a key of ``_TS``."""
    spec = _TS[grammar_lang]
    parser = _parser(spec[0])
    raw = _raw_lines(src)
    if parser is None:
        return {line: "module_code" for line in lines}
    root = _parse_memo(spec[0], parser, src.encode("utf-8")).root_node
    containers = spec[2]
    out: dict[int, str] = {}
    for line in lines:
        row = line - 1 - line0
        text = raw[row] if 0 <= row < len(raw) else ""
        if not text.strip():
            out[line] = "blank"
            continue
        col = len(text) - len(text.lstrip())
        byte_col = len(text[:col].encode("utf-8"))
        node = root.descendant_for_point_range((row, byte_col), (row, byte_col + 1))
        if node is None:
            out[line] = "module_code"
            continue
        if "comment" in node.type and (node.start_point[0] < row or node.start_point[1] <= byte_col):
            out[line] = "comment"
            continue
        chain, n = [], node
        while n is not None and n.id != root.id:
            chain.append(n)
            n = n.parent
        chain.reverse()                                   # outermost first, below the root
        if not chain:
            out[line] = "module_code"
        elif any(c.is_error or c.type == "ERROR" or c.is_missing for c in chain):
            out[line] = "unparsed"
        else:
            top = chain[0]
            kind = _kind_of(top.type)
            if kind == "constant" and _REQUIRE.match(_text(top, src.encode("utf-8"))):
                kind = "import"                                 # const x = require('y')
            elif kind == "export":                              # export interface ..., export const ...: what is exported
                decl = top.child_by_field_name("declaration") or next((c for c in top.named_children if "comment" not in c.type and "decorator" not in c.type), None)
                if decl is not None and _kind_of(decl.type) not in ("module_code", "comment"):
                    kind = _kind_of(decl.type)
            if top.type in containers:
                member = next((c for c in chain[1:] if c.parent is not None and c.parent.id != top.id and ("body" in c.parent.type or c.parent.type.endswith("declaration_list"))), None)
                if member is None:
                    kind = "class_header"
                else:
                    mk = _kind_of(member.type)
                    kind = mk if mk in ("import", "annotation", "comment") else "class_member"
            out[line] = kind
    return out


def _line_kinds(lang: str, path: str, src: str, lines: list[int]) -> dict[int, str]:
    """A kind for each of the given lines of ``src`` that lie outside every function: blank, comment, import, constant, class_member, class_header, type_definition, annotation, export,
    template (a Vue / Svelte part outside the scripts), module_code, or unparsed (inside a part the parser could not read)."""
    if not lines:
        return {}
    if lang == "python":
        try:
            return _python_line_kinds(src, lines)
        except (SyntaxError, ValueError, RecursionError):
            return {line: "unparsed" for line in lines}
    if lang in ("vue", "svelte"):
        raw = _raw_lines(src)
        blocks = list(_script_spans(src))
        masked = _HTML_COMMENT.sub(lambda m: "\x00" * len(m.group()), src)
        starts = [0] + [m.end() for m in re.finditer("\n", src)]
        out: dict[int, str] = {}
        for line in lines:
            text = raw[line - 1] if 0 < line <= len(raw) else ""
            if not text.strip():
                out[line] = "blank"
                continue
            pos = starts[line - 1]
            blk = next(((a, s, e) for a, s, e in blocks if s <= pos < e), None)
            if blk is None:
                out[line] = "comment" if masked[pos:pos + len(text)].strip("\x00 \t\r") == "" else "template"
                continue
            attrs, s, e = blk
            sub = "typescript" if re.search(r"lang\s*=\s*[\"']ts", attrs) else "javascript"
            out[line] = _tree_line_kinds(sub, src[s:e], [line], src.count("\n", 0, s)).get(line, "module_code")
        return out
    grammar_lang = "tsx" if lang == "typescript" and path.lower().endswith(".tsx") else lang
    try:
        grammar_lang = {"tsx": "tsx", "cpp": "cpp"}.get(parse_units(lang, path, src).grammar, grammar_lang)      # the grammar that read the file with the fewest errors
    except Unavailable:
        pass
    if grammar_lang not in _TS:
        return {line: "module_code" for line in lines}
    return _tree_line_kinds(grammar_lang, src, lines)


def _git_lines(diff: str | None) -> tuple[set[int], set[int]]:
    """The line numbers (1-based) a unified diff removes from the old file and adds to the new one."""
    removed, added = set(), set()
    old = new = 0
    for line in (diff or "").split("\n"):
        m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if m:
            old, new = int(m.group(1)), int(m.group(2))
            continue
        if not old and not new:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            added.add(new)
            new += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed.add(old)
            old += 1
        elif not line.startswith("\\"):
            old += 1
            new += 1
    return removed, added


MAX_AST_PAIRS = 80           # function pairs of one file that get an edit script; the rest say why not


def _unit_node(root, data: bytes, lo: int, hi: int):
    """The syntax node that is exactly a unit (its text between the byte offsets ``lo`` and ``hi``, leading indentation and trailing whitespace aside), or None."""
    while lo < hi and data[lo:lo + 1] in (b" ", b"\t"):
        lo += 1
    while hi > lo and data[hi - 1:hi] in (b" ", b"\t", b"\r", b"\n"):
        hi -= 1
    node = root.descendant_for_byte_range(lo, hi)
    while node is not None and node.parent is not None and node.parent.start_byte >= lo and node.parent.end_byte <= hi:
        node = node.parent
    if node is None or node.start_byte != lo or node.end_byte != hi:
        # a decorated Python function or an exported declaration may start before the unit text does
        return node if node is not None and node.start_byte <= lo and node.end_byte >= hi and node.end_byte - node.start_byte <= (hi - lo) + 40 else None
    return node


def _parse_for_edits(grammar: str, src: str):
    parser = _parser(grammar)
    if parser is None:
        return None
    data = src.encode("utf-8")
    try:
        return _parse_memo(grammar, parser, data).root_node, data
    except MemoryError:
        raise
    except Exception:  # ignored: no edit script for this pair
        return None


def ast_edits(lang: str, path: str, before: str, after: str, name: str | None = None, *, key_before: str | None = None, key_after: str | None = None, trees: dict | None = None) -> dict:
    """The syntax-level edit summary (insert, delete, update, move; see ``astdiff``) between a function of two versions of a file: the unit with the key ``name`` (or ``key_before`` /
    ``key_after`` when its key changed), found in the parse of each version. ``{"skipped": reason}`` when there is no grammar, no node for the unit, or a tree over ``astdiff.MAX_NODES``."""
    try:
        b, a = parse_pair(lang, path, before, after)
    except Unavailable:
        return {"skipped": "no_grammar"}
    kb, ka = key_before or name, key_after or name
    if kb not in b.info or ka not in a.info:
        return {"skipped": "unit_not_found", "algo": astdiff.ALGO}
    if lang in ("vue", "svelte"):
        return {"skipped": "script_block", "algo": astdiff.ALGO}
    out = []
    cache = trees if trees is not None else {}
    for side, src, units, key in (("b", before, b, kb), ("a", after, a, ka)):
        got = cache.get(side)
        if got is None:
            got = cache[side] = _parse_for_edits(units.grammar or lang, src) or False
        if got is False:
            return {"skipped": "no_grammar"}
        root, data = got
        node = _unit_node(root, data, *units.info[key].bytes)
        if node is None:
            return {"skipped": "no_unit_node", "algo": astdiff.ALGO}
        out.append(node)
    return astdiff.diff(out[0], out[1])


def deps_source(lang: str, units: _Units, root, data: bytes) -> splitdeps.Source:
    """A parsed version of a file in the form ``splitdeps`` reads (the tree and bytes ``ast_edits`` already has, the units, and what tells a container from a function)."""
    spec = _TS.get(units.grammar or lang) or _TS.get(lang)
    return splitdeps.Source(lang=lang, grammar=units.grammar or lang, root=root, data=data, units=units, containers=frozenset(spec[2]) if spec else frozenset(), name_of=_name, kind_of=_kind_of)


def pair_deps(lang: str, b: _Units, a: _Units, key_before: str, key_after: str, trees: dict, before: str, after: str) -> dict:
    """The dependency context of one function pair (see ``splitdeps``): the trees are parsed once per file and shared with the edit scripts through ``trees``."""
    if lang in ("vue", "svelte"):
        return {"skipped": "script_block"}
    sources, nodes = [], []
    for side, src, units, key in (("b", before, b, key_before), ("a", after, a, key_after)):
        got = trees.get(side)
        if got is None:
            got = trees[side] = _parse_for_edits(units.grammar or lang, src) or False
        if got is False:
            return {"skipped": "no_grammar"}
        root, data = got
        source = trees.get("src_" + side)
        if source is None:
            source = trees["src_" + side] = deps_source(lang, units, root, data)
        node = _unit_node(root, data, *units.info[key].bytes)
        if node is None:
            return {"skipped": "no_unit_node"}
        sources.append(source)
        nodes.append(node)
    try:
        return splitdeps.context(sources[0], nodes[0], key_before, sources[1], nodes[1], key_after)
    except RecursionError:
        return {"skipped": "too_deep"}


def _prov(info: UnitInfo) -> dict:
    return {"lines": info.lines, "bytes": info.bytes, "signature": info.signature, "kind": info.kind, "scope": info.scope, "block": info.block}


def _unit_view(info: UnitInfo, text: str, side: str) -> dict:
    return {"side": side, "key": info.key, "name": info.qual, "leaf": info.name, "kind": info.kind, "scope": info.scope, "signature": info.signature, "arity": info.arity,
            "lines": info.lines, "bytes": info.bytes, "text": text, "shash": info.shash, "nhash": info.nhash}


def analyze(lang: str, path: str, before: str, after: str, diff: str | None = None) -> Analysis:
    from app.backend.pipeline.acceleration.analysis_cache import analyze as cached
    return cached(_analyze_uncached, lang, path, before, after, diff)


def _analyze_uncached(lang: str, path: str, before: str, after: str, diff: str | None = None) -> Analysis:
    """Split one changed file and account for every changed line (see ``Analysis``). ``diff`` is the unified diff git recorded for the file; when given, its changed lines are checked
    against the ledger (``git_changed_lines`` and ``git_unaccounted`` in ``coverage``)."""
    rb, ra = _raw_lines(before), _raw_lines(after)
    ops = _ops(rb, ra)
    cb = [i + 1 for _, i1, i2, _, _ in ops for i in range(i1, i2)]
    ca = [j + 1 for _, _, _, j1, j2 in ops for j in range(j1, j2)]
    try:
        b, a = parse_pair(lang, path, before, after)
    except Unavailable as e:
        reason = str(e)
        an = Analysis([], "no_grammar" if reason.startswith("no_grammar") else "fallback", reason)
        _account(an, lang, path, before, after, rb, ra, ops, cb, ca, None, None, None, set(), set(), diff)
        return an
    result = match_units(lang, b, a)
    funcs = _changed_matches(b, a, [m for m in result.matches if m.kind in _SAME_NAME]) + _changed_matches(b, a, [m for m in result.matches if m.kind not in _SAME_NAME])
    partial = max(getattr(b, "errors", 0), getattr(a, "errors", 0))
    note = f"partial_errors:{partial}" if partial else None
    pairs = [{"granularity": "function", "function_name": q, "function_before": old, "span": None, "code_before": x, "code_after": y, "match": _match_dict(m),
              "prov": {"before": _prov(b.info[m.before]), "after": _prov(a.info[m.after])}} for q, x, y, old, m in funcs]
    an = Analysis(pairs, "ok", note)
    an.matches = result.matches
    an.parser.update(grammar_before=b.grammar, grammar_after=a.grammar, errors_before=b.errors, errors_after=a.errors, error_lines_before=b.error_lines, error_lines_after=a.error_lines,
                     recovered_regions_before=b.recovered, recovered_regions_after=a.recovered)
    for p, (_, _, _, _, m) in zip(pairs, funcs):
        bl, al = b.info[m.before].lines, a.info[m.after].lines
        if any(lo <= bl[1] and bl[0] <= hi for lo, hi in b.error_lines) or any(lo <= al[1] and al[0] <= hi for lo, hi in a.error_lines):
            p["match"]["signals"] = {**p["match"]["signals"], "parse_errors_in_unit": True}
    an.links = [{"kind": l.kind, "before": l.before, "after": l.after, "confidence": l.confidence, "signals": l.signals} for l in result.links]
    an.unpaired = ([_unit_view(b.info[k], b[k][0], "removed") for k in result.unmatched_before] + [_unit_view(a.info[k], a[k][0], "added") for k in result.unmatched_after])
    trees: dict = {}
    for n, (p, (_, _, _, _, m)) in enumerate(zip(pairs, funcs)):
        p["ast"] = (ast_edits(lang, path, before, after, key_before=m.before, key_after=m.after, trees=trees) if n < MAX_AST_PAIRS else {"skipped": "too_many_pairs", "algo": astdiff.ALGO})
        p["deps"] = (pair_deps(lang, b, a, m.before, m.after, trees, before, after) if n < splitdeps.MAX_PAIRS else {"skipped": "too_many_pairs"})
    emitted_b, emitted_a = {t[4].before for t in funcs}, {t[4].after for t in funcs}
    _account(an, lang, path, before, after, rb, ra, ops, cb, ca, b, a, result, emitted_b, emitted_a, diff)
    if not pairs:
        an.reason = "no_function_changed" + (f";{note}" if note else "")
    return an


def _account(an: Analysis, lang: str, path: str, before: str, after: str, rb, ra, ops, cb, ca, b, a, result, emitted_b: set, emitted_a: set, diff: str | None) -> None:
    """Classify every changed line once, build the residual and no-op regions, decide which regions get a hunk pair of their own, and add up the ledger.

    A line inside a function is accounted by that function (a pair, an added or removed function, or a function that only moved or was renamed). A line outside every function belongs
    to a residual region of some kind. With no function pair the changed regions with their context are the pairs, as they always were, and the residual regions they contain are
    emitted; with function pairs only the substantive residual regions get a hunk pair, with context that never takes in a function pair's lines."""
    cov = {"changed_lines": len(cb) + len(ca), **{k: 0 for k in BUCKETS}, "unaccounted": 0}
    units = b is not None
    own_b, own_a = (_owners(b.info, cb), _owners(a.info, ca)) if units else ({}, {})
    unmatched_b = set(result.unmatched_before) if units else set()
    unmatched_a = set(result.unmatched_after) if units else set()
    res_b: list[int] = []
    res_a: list[int] = []
    moved: set[tuple[str, str]] = set()
    for side, lines, own, emitted, unmatched in (("before", cb, own_b, emitted_b, unmatched_b), ("after", ca, own_a, emitted_a, unmatched_a)):
        for line in lines:
            key = own.get(line)
            if key is None:
                (res_b if side == "before" else res_a).append(line)
            elif key in emitted:
                cov["in_pairs"] += 1
            elif key in unmatched:
                cov["unpaired_units"] += 1
            else:
                cov["moved_units"] += 1
                moved.add((side, key))
    if units:
        for m in result.matches:
            if ("before", m.before) in moved or ("after", m.after) in moved:
                an.noop_regions.append({"before": b.info[m.before].lines, "after": a.info[m.after].lines, "reason": "renamed" if m.kind in ("renamed", "moved") else "moved"})
        kinds_b = _line_kinds(lang, path, before, res_b)
        kinds_a = _line_kinds(lang, path, after, res_a)
    else:
        kinds_b = {line: ("blank" if not rb[line - 1].strip() else "unsplit") for line in res_b}
        kinds_a = {line: ("blank" if not ra[line - 1].strip() else "unsplit") for line in res_a}
    # regions: per opcode and kind, the residual lines (n_lines counts changed lines, the spans may hold others)
    regions: dict[tuple[int, str], dict] = {}
    for n, (_, i1, i2, j1, j2) in enumerate(ops):
        for side, rng, kinds in (("before", range(i1 + 1, i2 + 1), kinds_b), ("after", range(j1 + 1, j2 + 1), kinds_a)):
            for line in rng:
                kind = kinds.get(line)
                if kind is None:
                    continue
                if kind == "blank":
                    cov["comment_or_blank"] += 1
                    continue
                r = regions.setdefault((n, kind), {"kind": kind, "before": None, "after": None, "n_lines": 0, "op": (i1, i2, j1, j2), "emitted": False})
                span = r[side]
                r[side] = (line, line) if span is None else (span[0], line)
                r["n_lines"] += 1
    blocked_b = {i for p in an.pairs if p["granularity"] == "function" for i in range(p["prov"]["before"]["lines"][0] - 1, p["prov"]["before"]["lines"][1])}
    blocked_a = {i for p in an.pairs if p["granularity"] == "function" for i in range(p["prov"]["after"]["lines"][0] - 1, p["prov"]["after"]["lines"][1])}
    function_pairs = bool(an.pairs)
    window_records = [] if function_pairs else _hunk_records(rb, ra, ops, lang)
    for (n, kind), r in sorted(regions.items(), key=lambda kv: (kv[0][0], (kv[1]["after"] or kv[1]["before"])[0], kv[0][1])):           # in file order of the new version
        r["text_before"] = "\n".join(rb[r["before"][0] - 1:r["before"][1]]) if r["before"] else ""
        r["text_after"] = "\n".join(ra[r["after"][0] - 1:r["after"][1]]) if r["after"] else ""
        if kind == "comment":
            an.noop_regions.append({"before": r["before"], "after": r["after"], "reason": "comment"})
            cov["comment_or_blank"] += r["n_lines"]
            continue
        i1, i2, j1, j2 = r["op"]
        if function_pairs:
            if kind in SUBSTANTIVE:
                bs = (r["before"][0] - 1, r["before"][1]) if r["before"] else (i1, i1)
                as_ = (r["after"][0] - 1, r["after"][1]) if r["after"] else (j1, j1)
                lo_b, hi_b, lo_a, hi_a = _window(rb, ra, bs[0], bs[1], as_[0], as_[1], CONTEXT, blocked_b, blocked_a)
                bt, at = "\n".join(rb[lo_b:hi_b]), "\n".join(ra[lo_a:hi_a])
                if bt.strip() and at.strip() and not is_noop(lang, bt, at):
                    an.pairs.append({"granularity": "hunk", "function_name": None, "function_before": None, "span": f"{lo_b + 1}-{max(hi_b, lo_b + 1)}", "code_before": bt, "code_after": at,
                                     "role": "residual", "residual_kind": kind,
                                     "prov": {"before": {"lines": (lo_b + 1, max(hi_b, lo_b + 1))}, "after": {"lines": (lo_a + 1, max(hi_a, lo_a + 1))}}})
                    r["emitted"] = True
        else:
            win = next((w for w in window_records if w["ops"][0] <= i1 and i2 <= w["ops"][1] and w["ops"][2] <= j1 and j2 <= w["ops"][3]), None)
            r["emitted"] = bool(win and win["usable"])
            if win is not None and win["noop"]:
                an.noop_regions.append({"before": r["before"], "after": r["after"], "reason": "format"})
                cov["comment_or_blank"] += r["n_lines"]
                continue
        an.residual.append(r)
        cov["residual_emitted" if r["emitted"] else "residual_recorded"] += r["n_lines"]
    if not function_pairs:
        usable = [w for w in window_records if w["usable"]]
        whole = len(usable) == 1 and usable[0]["before"].replace("\r\n", "\n") == before.replace("\r\n", "\n").strip("\n") and usable[0]["after"].replace("\r\n", "\n") == after.replace("\r\n", "\n").strip("\n")
        if whole:
            for r in an.residual:                       # the one hunk is the whole file: the stored file-level pair carries it, and no sub-pair is added
                if r["emitted"]:
                    r["emitted"] = False
                    cov["residual_emitted"] -= r["n_lines"]
                    cov["residual_recorded"] += r["n_lines"]
        else:
            an.pairs = [{"granularity": "hunk", "function_name": None, "function_before": None, "span": f"{w['win'][0] + 1}-{max(w['win'][1], w['win'][0] + 1)}",
                         "code_before": w["before"], "code_after": w["after"], "prov": {"before": {"lines": (w["win"][0] + 1, max(w["win"][1], w["win"][0] + 1))},
                                                                                         "after": {"lines": (w["win"][2] + 1, max(w["win"][3], w["win"][2] + 1))}}} for w in usable]
    cov["unaccounted"] = cov["changed_lines"] - sum(cov[k] for k in BUCKETS)
    if diff:
        gb, ga = _git_lines(diff)
        sb, sa = set(cb), set(ca)
        extra_b, extra_a = [l for l in gb if l not in sb], [l for l in ga if l not in sa]
        ob, oa = (_owners(b.info, extra_b), _owners(a.info, extra_a)) if units else ({}, {})
        cov["git_changed_lines"] = len(gb) + len(ga)
        cov["git_unaccounted"] = sum(1 for l in extra_b if l not in ob) + sum(1 for l in extra_a if l not in oa)
    an.coverage = cov


class _CrossUnit:
    """A unit of one file as the cross-file matcher sees it (the attributes ``splitmatch`` reads, and the file it came from)."""
    __slots__ = ("key", "qual", "name", "scope", "signature", "arity", "lines", "shash", "nhash", "kind", "text", "file", "view")

    def __init__(self, view: dict, file: int, key: str):
        self.key, self.qual, self.name, self.scope = key, view["name"], view["leaf"], view["scope"]
        self.signature, self.arity, self.lines, self.shash, self.nhash = view["signature"], view["arity"], view["lines"], view["shash"], view["nhash"]
        self.kind, self.text, self.file, self.view = view["kind"], view["text"], file, view


def link_cross_file(entries: list[dict]) -> list[dict]:
    """Link the functions of one commit that left a file to the functions that arrived in another.

    ``entries`` are ``{"path", "old_path", "lang", "analysis"}`` for the changed files of the commit (``analysis`` from ``analyze``). Only the functions that have no counterpart in their own
    file take part (``analysis.unpaired``), so a function removed from one file and added to another is the one case that is left. They are matched in two stages of ``splitmatch``: the same
    syntax tree under any name (a pure move), the same name and parameter list, and alike bodies, never within one file, never across languages. Each result is
    ``{"kind": "cross_file_move", "removed_path", "added_path", "removed", "added", "confidence", "signals", "changed", "removed_index", "added_index"}``; ``changed`` is False for a move
    that left the code as it was (nothing to learn from it as a pair). The cases that are not one to one come back as ``cross_file_ambiguous`` / ``cross_file_split`` /
    ``cross_file_merge`` with the units named in ``before`` / ``after``."""
    by_lang: dict[str, list[int]] = collections.defaultdict(list)
    for i, e in enumerate(entries):
        by_lang[e["lang"]].append(i)
    out: list[dict] = []
    for lang, idx in by_lang.items():
        if len(idx) < 2:
            continue
        removed: dict[str, _CrossUnit] = {}
        added: dict[str, _CrossUnit] = {}
        for i in idx:
            for u in entries[i]["analysis"].unpaired:
                key = f"{i}::{u['key']}"
                (removed if u["side"] == "removed" else added)[key] = _CrossUnit(u, i, key)
        if not removed or not added or len({u.file for u in removed.values()} | {u.file for u in added.values()}) < 2:
            continue
        res = splitmatch.match_leftovers(removed, added, lambda k: _content_tokens(lang, removed[k], removed[k].text), lambda k: _content_tokens(lang, added[k], added[k].text),
                                         allow=lambda r, a: removed[r].file != added[a].file)
        for m in res.matches:
            rb, ra = removed[m.before], added[m.after]
            out.append({"kind": "cross_file_move", "removed_path": entries[rb.file]["old_path"], "added_path": entries[ra.file]["path"], "removed": rb.view, "added": ra.view,
                        "confidence": m.confidence, "signals": m.signals, "changed": rb.nhash != ra.nhash, "removed_index": rb.file, "added_index": ra.file})
        for l in res.links:
            out.append({"kind": f"cross_file_{l.kind}", "before": [removed[k].view["name"] for k in l.before], "after": [added[k].view["name"] for k in l.after],
                        "before_paths": [entries[removed[k].file]["old_path"] for k in l.before], "after_paths": [entries[added[k].file]["path"] for k in l.after], "confidence": l.confidence})
    return sorted(out, key=lambda x: (x["kind"], x.get("removed_path", ""), x.get("added_path", ""), (x.get("removed") or {}).get("name", "")))


def split_with_status(lang: str, path: str, before: str, after: str) -> tuple[list[dict], str, str | None]:
    """(pairs, status, reason): sub-file pairs of one changed file and how the extraction went.
    status is ``ok`` (parsed; reason ``no_function_changed`` when only top-level code changed, and ``partial_errors:N`` when the best parse still had N error nodes, so the units may be incomplete),
    ``no_grammar`` (the language has no parser) or ``fallback`` (a parser exists but failed or is
    unavailable: ``python_syntax_error``, ``tree_sitter_error``, ``grammar_unavailable:<lang>``).
    Function pairs carry ``function_before`` for a renamed function, ``match`` (kind, confidence, signals) and ``prov`` (lines and bytes of both versions)."""
    a = analyze(lang, path, before, after)
    return a.pairs, a.status, a.reason


def _match_dict(m: splitmatch.Match) -> dict:
    """A match as stored beside a pair (plain data)."""
    return {"kind": m.kind, "confidence": m.confidence, "signals": m.signals, "alternatives": m.alternatives}


def split(lang: str, path: str, before: str, after: str) -> list[dict]:
    """Sub-file pairs of one changed file: functions when any changed (and the
    language parses), else hunks. Each: {granularity, function_name, span,
    code_before, code_after}."""
    return split_with_status(lang, path, before, after)[0]
