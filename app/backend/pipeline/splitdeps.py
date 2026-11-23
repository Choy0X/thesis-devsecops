"""What a changed function depends on, as far as the file itself shows it.

For each function pair ``funcsplit.analyze`` calls ``context`` with the syntax trees it already has. The result is kept apart from the code (``pair["deps"]``) and describes the
file and the function **as they were before the change** (the vulnerable version), so it can be given as context without the fix leaking into it. Only ``delta`` talks about the fix.

What is resolved, and how far:

* ``imports``    the import bindings the function uses, with the module they name. The imported module is never opened; only the statement is verified.
* ``constants``  module-level constants and variables (and, for Java, static or final fields of the enclosing classes) the function uses, with their text.
* ``types``      classes, interfaces, structs, enums and aliases of the file the function names, with their header or definition.
* ``functions``  functions of the same file the function names directly (callers' callees one level down; nothing is followed further), with their spans.
* ``enclosing``  the nearest class, impl, namespace or module, with its header.
* ``unresolved`` names the function calls or uses as types that none of the above explain (a wildcard import, a global, a builtin of a library, another file). They are *not* guesses
  about where the name comes from.

Languages without a symbol reader (``text_only``) get the import statements as text, the module declaration and the enclosing class, and no resolution at all. A name bound anywhere in
the function (a parameter, an assignment, a loop variable) counts as local to it; this is an approximation that is documented, not hidden. Nothing here reads another file, follows an
import, or expands a dependency's own dependencies.
"""
from __future__ import annotations

import builtins as _builtins
import re
from dataclasses import dataclass, field
from typing import Callable

DEPS_VERSION = 1
MAX_LISTED = 24          # most imports, unresolved names and delta entries listed per pair
MAX_CONSTANTS = 8
MAX_TYPES = 8
MAX_FUNCTIONS = 12
TEXT_CHARS = 200         # a constant's text
TYPE_CHARS = 300         # a type's header or definition
IMPORT_TEXT_CHARS = 120  # one import statement in the text-only form
MAX_SCAN_NODES = 40_000  # syntax nodes read in one function; beyond it the references of the rest are not collected
MAX_TOP_NODES = 20_000   # top-level statements read per file
MAX_PAIRS = 200          # function pairs of one file that get a context; the rest say why not

TEXT_ONLY_HINT = "no symbol reader for this language: import statements and the enclosing class only"


@dataclass(frozen=True)
class Decl:
    name: str
    kind: str
    text: str = ""
    line: int = 0
    source: str = ""        # the module an import names
    member: str = ""        # the name taken from that module, when it differs from the bound name


class Symbols:
    """The declarations of one version of a file that a function can refer to. ``resolution`` is ``ast`` or ``text_only``."""

    def __init__(self, resolution: str):
        self.resolution = resolution
        self.imports: dict[str, Decl] = {}
        self.import_order: list[Decl] = []
        self.wildcards: list[str] = []
        self.includes: list[str] = []
        self.constants: dict[str, Decl] = {}
        self.types: dict[str, Decl] = {}
        self.members: dict[str, dict[str, Decl]] = {}         # class qualified name -> fields (Java)
        self.protos: dict[str, Decl] = {}                     # C declarations without a body
        self.functions: dict[tuple[str, str], list] = {}      # (scope, leaf name) -> [UnitInfo]
        self.module = ""
        self.import_text: list[str] = []
        self.import_total = 0

    def add_import(self, d: Decl) -> None:
        self.import_order.append(d)
        self.imports.setdefault(d.name, d)

    def add_constant(self, d: Decl) -> None:
        self.constants.setdefault(d.name, d)

    def add_type(self, d: Decl) -> None:
        self.types.setdefault(d.name, d)


@dataclass
class Source:
    """One version of a file as ``funcsplit`` read it: the syntax tree, its bytes and its units."""
    lang: str
    grammar: str
    root: object
    data: bytes
    units: object                       # funcsplit._Units: key -> (text, nested), .info[key] -> UnitInfo
    containers: frozenset               # node types of enclosing classes, impls and namespaces
    name_of: Callable
    kind_of: Callable
    symbols: Symbols | None = field(default=None, repr=False)


# --- small helpers -----------------------------------------------------------------------------------------------------------------------------------------------------------

def _text(node, data: bytes) -> str:
    return data[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _flat(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:max(0, limit - 3)] + "..."


def _children(node) -> list[tuple[object, str | None]]:
    """Every child with the name of its field, in order."""
    out = []
    cur = node.walk()
    if cur.goto_first_child():
        while True:
            out.append((cur.node, cur.field_name))
            if not cur.goto_next_sibling():
                break
    return out


def _by_field(node, name: str) -> list:
    return [c for c, f in _children(node) if f == name]


def _line(node) -> int:
    return node.start_point[0] + 1


def _head(node, data: bytes, limit: int) -> str:
    """A declaration up to where its body begins (a whole short declaration when it has no body)."""
    body = node.child_by_field_name("body")
    end = body.start_byte if body is not None and body.start_byte > node.start_byte else node.end_byte
    return _flat(data[node.start_byte:min(end, node.start_byte + 4 * limit)].decode("utf-8", "replace"), limit)


def _string_value(node, data: bytes) -> str:
    t = _text(node, data).strip()
    return t[1:-1] if len(t) >= 2 and t[0] in "'\"`" and t[-1] == t[0] else t


# --- rules for reading the references of a function --------------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Rules:
    ident: frozenset                               # node types that name a variable, function or module
    types: frozenset = frozenset()                 # node types that name a type
    binders: frozenset = frozenset()               # further node types that can be a bound name (a pattern identifier)
    bind: dict = field(default_factory=dict)       # node type -> fields (None: all named children) whose patterns bind names
    descend: dict = field(default_factory=dict)    # pattern node type -> "*" (all named children), "a,b" (these fields) or "-a" (all but field a)
    skip: frozenset = frozenset()                  # (parent type, field) that holds a name that is not a reference (a property, a keyword argument)
    skip_in: frozenset = frozenset()               # (parent type, the parent's own field) whose name children are not references (the key of a composite literal)
    calls: frozenset = frozenset()                 # (parent type, field) that holds the callee's bare name
    prune: frozenset = frozenset()                 # subtrees that are not read
    builtins: frozenset = frozenset()
    bare_members: bool = False                     # a bare name can be a method or field of the enclosing class (Java)


_PY = Rules(
    ident=frozenset({"identifier"}),
    bind={"parameters": (None,), "lambda_parameters": (None,), "assignment": ("left",), "augmented_assignment": ("left",), "for_statement": ("left",), "for_in_clause": ("left",),
          "named_expression": ("name",), "default_parameter": ("name",), "typed_default_parameter": ("name",), "function_definition": ("name",), "class_definition": ("name",),
          "as_pattern_target": (None,)},
    descend={"pattern_list": "*", "tuple_pattern": "*", "list_pattern": "*", "tuple": "*", "list": "*", "list_splat_pattern": "*", "dictionary_splat_pattern": "*",
             "typed_parameter": "*", "as_pattern_target": "*"},
    skip=frozenset({("attribute", "attribute"), ("keyword_argument", "name")}),
    calls=frozenset({("call", "function")}),
    prune=frozenset({"global_statement", "nonlocal_statement", "import_statement", "import_from_statement", "future_import_statement"}),
    builtins=frozenset(dir(_builtins)) | {"__file__", "__name__", "__doc__"},
)

_JS_BUILTINS = frozenset("""Math JSON Object Array String Number Boolean Promise Date RegExp Error TypeError RangeError SyntaxError ReferenceError EvalError URIError Map Set WeakMap WeakSet Symbol
BigInt Buffer process console global globalThis window document navigator location undefined NaN Infinity parseInt parseFloat isNaN isFinite setTimeout clearTimeout setInterval clearInterval
setImmediate require module exports __dirname __filename URL URLSearchParams Intl Reflect Proxy Uint8Array Int8Array Uint16Array Int16Array Uint32Array Int32Array Float32Array Float64Array
ArrayBuffer DataView encodeURIComponent decodeURIComponent encodeURI decodeURI escape unescape eval fetch Function structuredClone queueMicrotask TextEncoder TextDecoder AbortController
Record Partial Readonly Pick Omit Required Awaited ReturnType Exclude Extract NonNullable Parameters arguments""".split())

_JS = Rules(
    ident=frozenset({"identifier", "shorthand_property_identifier"}),
    types=frozenset({"type_identifier"}),
    binders=frozenset({"shorthand_property_identifier_pattern"}),
    bind={"variable_declarator": ("name",), "formal_parameters": (None,), "arrow_function": ("parameter",), "for_in_statement": ("left",), "catch_clause": ("parameter",),
          "function_declaration": ("name",), "function_expression": ("name",), "generator_function_declaration": ("name",), "class_declaration": ("name",), "class": ("name",),
          "type_parameter": ("name",)},
    descend={"required_parameter": "pattern", "optional_parameter": "pattern", "assignment_pattern": "left", "object_assignment_pattern": "left", "rest_pattern": "*",
             "object_pattern": "*", "array_pattern": "*", "pair_pattern": "value"},
    skip=frozenset({("nested_type_identifier", "name")}),
    calls=frozenset({("call_expression", "function"), ("new_expression", "constructor")}),
    builtins=_JS_BUILTINS,
)

_JAVA_BUILTINS = frozenset("""String Object Integer Long Double Float Short Byte Character Boolean Number Math System Thread Runnable Exception RuntimeException Error Throwable
IllegalArgumentException IllegalStateException NullPointerException IndexOutOfBoundsException UnsupportedOperationException ArithmeticException ClassCastException Class Iterable Comparable
CharSequence StringBuilder StringBuffer Override Deprecated SuppressWarnings FunctionalInterface SafeVarargs Void Enum Record AutoCloseable Cloneable InterruptedException SecurityException
AssertionError StackOverflowError OutOfMemoryError Process ProcessBuilder Runtime ThreadLocal""".split())

_JAVA = Rules(
    ident=frozenset({"identifier"}),
    types=frozenset({"type_identifier"}),
    bind={"formal_parameter": ("name",), "spread_parameter": ("name",), "catch_formal_parameter": ("name",), "enhanced_for_statement": ("name",), "variable_declarator": ("name",),
          "resource": ("name",), "lambda_expression": ("parameters",), "instanceof_expression": ("name",), "type_parameter": (None,), "method_declaration": ("name",),
          "constructor_declaration": ("name",), "class_declaration": ("name",)},
    descend={"formal_parameters": "*", "inferred_parameters": "*"},
    skip=frozenset({("field_access", "field"), ("element_value_pair", "key"), ("method_reference", "name")}),
    calls=frozenset(),
    prune=frozenset({"scoped_type_identifier", "scoped_identifier"}),
    builtins=_JAVA_BUILTINS,
    bare_members=True,
)

_GO_BUILTINS = frozenset("""len cap make new append copy delete close panic recover print println complex real imag min max clear string int int8 int16 int32 int64 uint uint8 uint16 uint32
uint64 uintptr byte rune float32 float64 complex64 complex128 bool error any comparable nil true false iota""".split())

_GO = Rules(
    ident=frozenset({"identifier", "package_identifier"}),
    types=frozenset({"type_identifier"}),
    bind={"parameter_declaration": ("name",), "variadic_parameter_declaration": ("name",), "short_var_declaration": ("left",), "var_spec": ("name",), "const_spec": ("name",),
          "range_clause": ("left",), "type_switch_statement": ("alias",), "function_declaration": ("name",), "type_parameter_declaration": ("name",)},
    descend={"expression_list": "*"},
    skip=frozenset({("qualified_type", "name")}),
    skip_in=frozenset({("literal_element", "key")}),
    calls=frozenset({("call_expression", "function")}),
    builtins=_GO_BUILTINS,
)

_RUST_BUILTINS = frozenset("""Some None Ok Err Option Result Vec String Box Self Default Clone Copy Send Sync Sized Drop Fn FnMut FnOnce Iterator IntoIterator Extend From Into AsRef AsMut ToString
ToOwned PartialEq Eq PartialOrd Ord Debug Hash drop std core alloc crate super self""".split())

_RUST = Rules(
    ident=frozenset({"identifier"}),
    types=frozenset({"type_identifier"}),
    binders=frozenset({"shorthand_field_identifier"}),
    bind={"parameter": ("pattern",), "let_declaration": ("pattern",), "let_condition": ("pattern",), "for_expression": ("pattern",), "match_pattern": (None,),
          "closure_parameters": (None,), "function_item": ("name",), "type_parameter": ("name",), "const_parameter": ("name",)},
    descend={"tuple_pattern": "*", "tuple_struct_pattern": "-type", "struct_pattern": "-type", "field_pattern": "*", "reference_pattern": "*", "mut_pattern": "*", "or_pattern": "*",
             "slice_pattern": "*", "ref_pattern": "*", "captured_pattern": "*", "remaining_field_pattern": "*"},
    skip=frozenset({("scoped_identifier", "name"), ("scoped_type_identifier", "name"), ("macro_invocation", "macro")}),
    calls=frozenset({("call_expression", "function")}),
    prune=frozenset({"use_declaration", "token_tree", "attribute_item", "inner_attribute_item"}),
    builtins=_RUST_BUILTINS,
)

_C_BUILTINS = frozenset("""NULL EOF size_t ssize_t uint8_t uint16_t uint32_t uint64_t int8_t int16_t int32_t int64_t bool true false errno stdin stdout stderr FILE va_list uintptr_t intptr_t
ptrdiff_t off_t wchar_t""".split())

_C = Rules(
    ident=frozenset({"identifier"}),
    types=frozenset({"type_identifier"}),
    bind={"parameter_declaration": ("declarator",), "declaration": ("declarator",), "function_definition": ("declarator",)},
    descend={"init_declarator": "declarator", "pointer_declarator": "declarator", "array_declarator": "declarator", "function_declarator": "declarator",
             "parenthesized_declarator": "*", "reference_declarator": "*", "attributed_declarator": "*"},
    calls=frozenset({("call_expression", "function")}),
    builtins=_C_BUILTINS,
)

RULES = {"python": _PY, "javascript": _JS, "typescript": _JS, "java": _JAVA, "go": _GO, "rust": _RUST, "c": _C}


def _spec_children(node, spec: str) -> list:
    kids = _children(node)
    if spec == "*":
        return [c for c, _ in kids if c.is_named]
    if spec.startswith("-"):
        skip = set(spec[1:].split(","))
        return [c for c, f in kids if c.is_named and f not in skip]
    want = set(spec.split(","))
    return [c for c, f in kids if f in want]


def _collect(node, rules: Rules, out: list, depth: int = 0) -> None:
    """The name nodes a binding pattern introduces (not the properties, subscripts or default values inside it)."""
    t = node.type
    if t in rules.ident or t in rules.types or t in rules.binders:
        out.append(node)
        return
    spec = rules.descend.get(t)
    if spec is not None and depth < 8:
        for c in _spec_children(node, spec):
            _collect(c, rules, out, depth + 1)


# --- the declarations of a file ----------------------------------------------------------------------------------------------------------------------------------------------

def _py_import_decls(n, data: bytes) -> tuple[list[Decl], list[str]]:
    out: list[Decl] = []
    wild: list[str] = []
    line = _line(n)
    if n.type == "import_statement":
        for c in _by_field(n, "name"):
            if c.type == "dotted_name":
                full = _text(c, data)
                out.append(Decl(full.split(".")[0], "import", source=full, line=line))
            elif c.type == "aliased_import":
                name, alias = c.child_by_field_name("name"), c.child_by_field_name("alias")
                if name is not None and alias is not None:
                    out.append(Decl(_text(alias, data), "import", source=_text(name, data), line=line))
    elif n.type == "import_from_statement":
        mod = n.child_by_field_name("module_name")
        source = _text(mod, data) if mod is not None else ""
        for c, f in _children(n):
            if c.type == "wildcard_import":
                wild.append(source)
            elif f == "name" and c.type == "dotted_name":
                member = _text(c, data)
                out.append(Decl(member.split(".")[-1], "import", source=source, member=member, line=line))
            elif f == "name" and c.type == "aliased_import":
                name, alias = c.child_by_field_name("name"), c.child_by_field_name("alias")
                if name is not None and alias is not None:
                    out.append(Decl(_text(alias, data), "import", source=source, member=_text(name, data), line=line))
    return out, wild


_PY_COMPOUND = {"if_statement", "elif_clause", "else_clause", "try_statement", "except_clause", "finally_clause", "with_statement", "block", "except_group_clause"}


def _read_python(root, data: bytes, sym: Symbols) -> None:
    budget = [MAX_TOP_NODES]

    def visit(nodes, depth: int) -> None:
        for n in nodes:
            budget[0] -= 1
            if budget[0] < 0:
                return
            t = n.type
            if t == "expression_statement" and n.named_children:
                n, t = n.named_children[0], n.named_children[0].type
            if t in ("import_statement", "import_from_statement"):
                decls, wild = _py_import_decls(n, data)
                for d in decls:
                    sym.add_import(d)
                sym.wildcards.extend(wild)
            elif t == "assignment":
                left = n.child_by_field_name("left")
                if left is None:
                    continue
                found: list = []
                _collect(left, _PY, found)
                text = _flat(_text(n, data), TEXT_CHARS)
                for f in found:
                    name = _text(f, data)
                    sym.add_constant(Decl(name, "constant" if name.isupper() else "variable", text, _line(n)))
            elif t in ("class_definition", "decorated_definition"):
                d = n.child_by_field_name("definition") if t == "decorated_definition" else n
                if d is not None and d.type == "class_definition":
                    name = d.child_by_field_name("name")
                    if name is not None:
                        sym.add_type(Decl(_text(name, data), "class", _head(d, data, TYPE_CHARS), _line(d)))
            elif t in _PY_COMPOUND and depth < 4:
                visit([c for c in n.named_children], depth + 1)

    visit(root.named_children, 0)


def _js_import(n, data: bytes, sym: Symbols) -> None:
    src = n.child_by_field_name("source")
    source = _string_value(src, data) if src is not None else ""
    line = _line(n)
    clause = next((c for c in n.named_children if c.type == "import_clause"), None)
    if clause is None:
        sym.import_total += 1
        return
    for c in clause.named_children:
        if c.type == "identifier":
            sym.add_import(Decl(_text(c, data), "import", source=source, member="default", line=line))
        elif c.type == "namespace_import":
            for i in c.named_children:
                if i.type == "identifier":
                    sym.add_import(Decl(_text(i, data), "import", source=source, member="*", line=line))
        elif c.type == "named_imports":
            for s in c.named_children:
                if s.type != "import_specifier":
                    continue
                name, alias = s.child_by_field_name("name"), s.child_by_field_name("alias")
                if name is not None:
                    sym.add_import(Decl(_text(alias if alias is not None else name, data), "import", source=source, member=_text(name, data), line=line))


def _js_declarators(n, data: bytes, sym: Symbols) -> None:
    for d in n.named_children:
        if d.type != "variable_declarator":
            continue
        name, value = d.child_by_field_name("name"), d.child_by_field_name("value")
        line = _line(n)
        required = None
        member = ""
        v = value
        if v is not None and v.type == "member_expression":                                    # require('x').y
            obj, prop = v.child_by_field_name("object"), v.child_by_field_name("property")
            if obj is not None and obj.type == "call_expression" and prop is not None:
                member, v = _text(prop, data), obj
        if v is not None and v.type == "call_expression":
            fn, args = v.child_by_field_name("function"), v.child_by_field_name("arguments")
            if fn is not None and fn.type == "identifier" and _text(fn, data) == "require" and args is not None and args.named_children and args.named_children[0].type == "string":
                required = _string_value(args.named_children[0], data)
        if name is None:
            continue
        found: list = []
        _collect(name, _JS, found)
        if required is not None:
            if name.type == "identifier":
                sym.add_import(Decl(_text(name, data), "import", source=required, member=member, line=line))
            else:
                for f in _spec_children(name, "*"):
                    if f.type == "shorthand_property_identifier_pattern":
                        sym.add_import(Decl(_text(f, data), "import", source=required, member=_text(f, data), line=line))
                    elif f.type == "pair_pattern":
                        key, val = f.child_by_field_name("key"), f.child_by_field_name("value")
                        if key is not None and val is not None and val.type == "identifier":
                            sym.add_import(Decl(_text(val, data), "import", source=required, member=_text(key, data), line=line))
            continue
        prefix = data[n.start_byte:d.start_byte].decode("utf-8", "replace").strip()
        keyword = prefix.split()[-1] if prefix.split() else ""
        text = _flat(f"{keyword} {_text(d, data)}", TEXT_CHARS)
        for f in found:
            nm = _text(f, data)
            sym.add_constant(Decl(nm, "constant" if keyword == "const" else "variable", text, line))


_JS_TYPES = {"class_declaration": "class", "abstract_class_declaration": "class", "interface_declaration": "interface", "type_alias_declaration": "type", "enum_declaration": "enum"}


def _read_js(root, data: bytes, sym: Symbols) -> None:
    for i, n in enumerate(root.named_children):
        if i >= MAX_TOP_NODES:
            break
        if n.type == "export_statement":
            n = n.child_by_field_name("declaration") or next((c for c in n.named_children if c.type in _JS_TYPES or c.type in ("lexical_declaration", "variable_declaration")), None)
            if n is None:
                continue
        t = n.type
        if t == "import_statement":
            _js_import(n, data, sym)
        elif t in ("lexical_declaration", "variable_declaration"):
            _js_declarators(n, data, sym)
        elif t in _JS_TYPES:
            name = n.child_by_field_name("name")
            if name is not None:
                sym.add_type(Decl(_text(name, data), _JS_TYPES[t], _head(n, data, TYPE_CHARS), _line(n)))


_JAVA_TYPES = {"class_declaration": "class", "interface_declaration": "interface", "enum_declaration": "enum", "record_declaration": "record", "annotation_type_declaration": "annotation"}


def _java_members(owner, data: bytes, qual: str, sym: Symbols, depth: int = 0) -> None:
    body = owner.child_by_field_name("body")
    if body is None or depth > 6:
        return
    for n in body.named_children:
        if n.type == "field_declaration":
            mods = next((c for c in n.named_children if c.type == "modifiers"), None)
            words = set(re.findall(r"\w+", _text(mods, data))) if mods is not None else set()
            if not words & {"final", "static"}:
                continue
            for d in _by_field(n, "declarator"):
                name = d.child_by_field_name("name")
                if name is not None:
                    kind = "constant" if "final" in words else "static"
                    sym.members.setdefault(qual, {}).setdefault(_text(name, data), Decl(_text(name, data), kind, _flat(_text(n, data), TEXT_CHARS), _line(n)))
        elif n.type in _JAVA_TYPES:
            name = n.child_by_field_name("name")
            if name is not None:
                nested = f"{qual}.{_text(name, data)}"
                sym.add_type(Decl(_text(name, data), _JAVA_TYPES[n.type], _head(n, data, TYPE_CHARS), _line(n)))
                _java_members(n, data, nested, sym, depth + 1)


def _read_java(root, data: bytes, sym: Symbols) -> None:
    for i, n in enumerate(root.named_children):
        if i >= MAX_TOP_NODES:
            break
        t = n.type
        if t == "package_declaration":
            sym.module = _flat(_text(n, data).replace("package", "", 1).strip(" ;\n\r\t"), 120)
        elif t == "import_declaration":
            ident = next((c for c in n.named_children if c.type == "scoped_identifier"), None)
            if ident is None:
                continue
            full = _text(ident, data)
            if any(c.type == "asterisk" for c in n.named_children):
                sym.wildcards.append(full)
                continue
            head, _, last = full.rpartition(".")
            static = any(c.type == "static" for c in n.children)
            sym.add_import(Decl(last, "import", source=head if not static else head, member="static" if static else "", line=_line(n)))
        elif t in _JAVA_TYPES:
            name = n.child_by_field_name("name")
            if name is not None:
                sym.add_type(Decl(_text(name, data), _JAVA_TYPES[t], _head(n, data, TYPE_CHARS), _line(n)))
                _java_members(n, data, _text(name, data), sym)


def _go_path_name(path: str) -> str:
    parts = [p for p in path.split("/") if p]
    if not parts:
        return path
    last = parts[-1]
    if re.fullmatch(r"v\d+", last) and len(parts) > 1:
        last = parts[-2]
    return re.sub(r"\.v\d+$", "", last)


def _read_go(root, data: bytes, sym: Symbols) -> None:
    def spec(s, line: int) -> None:
        path = s.child_by_field_name("path")
        if path is None:
            return
        source = _string_value(path, data)
        name = s.child_by_field_name("name")
        if name is None:
            sym.add_import(Decl(_go_path_name(source), "import", source=source, line=line))
        elif name.type == "package_identifier":
            sym.add_import(Decl(_text(name, data), "import", source=source, line=line))
        elif name.type == "dot":
            sym.wildcards.append(source)
        else:                                                   # blank import: bound to no name
            sym.import_total += 1

    for i, n in enumerate(root.named_children):
        if i >= MAX_TOP_NODES:
            break
        t = n.type
        if t == "package_clause":
            sym.module = _flat(_text(n, data).replace("package", "", 1), 120)
        elif t == "import_declaration":
            for c in n.named_children:
                if c.type == "import_spec":
                    spec(c, _line(c))
                elif c.type == "import_spec_list":
                    for s in c.named_children:
                        if s.type == "import_spec":
                            spec(s, _line(s))
        elif t in ("const_declaration", "var_declaration"):
            kind = "constant" if t == "const_declaration" else "variable"
            specs = []
            for c in n.named_children:
                if c.type in ("const_spec", "var_spec"):
                    specs.append(c)
                elif c.type == "var_spec_list":
                    specs.extend(s for s in c.named_children if s.type == "var_spec")
            for s in specs:
                text = _flat(_text(s, data), TEXT_CHARS)
                for name in _by_field(s, "name"):
                    if name.type == "identifier":
                        sym.add_constant(Decl(_text(name, data), kind, text, _line(s)))
        elif t == "type_declaration":
            for s in n.named_children:
                if s.type in ("type_spec", "type_alias"):
                    name = s.child_by_field_name("name")
                    ty = s.child_by_field_name("type")
                    kind = {"struct_type": "struct", "interface_type": "interface"}.get(ty.type if ty is not None else "", "type")
                    if name is not None:
                        sym.add_type(Decl(_text(name, data), kind, _flat(_text(s, data), TYPE_CHARS), _line(s)))


def _rust_use(n, prefix: str, data: bytes, sym: Symbols, line: int, depth: int = 0) -> None:
    if depth > 6:
        return
    t = n.type
    if t == "identifier":
        name = _text(n, data)
        sym.add_import(Decl(name, "import", source=prefix.rstrip(":") if prefix else name, member=name if prefix else "", line=line))
    elif t == "scoped_identifier":
        full = _text(n, data)
        head, _, last = full.rpartition("::")
        sym.add_import(Decl(last, "import", source=(prefix + head), member=last, line=line))
    elif t == "scoped_use_list":
        path = n.child_by_field_name("path")
        lst = n.child_by_field_name("list")
        base = prefix + (_text(path, data) + "::" if path is not None else "")
        if lst is not None:
            for c in lst.named_children:
                if c.type == "self":
                    sym.add_import(Decl(base.rstrip(":").split("::")[-1], "import", source=base.rstrip(":"), line=line))
                else:
                    _rust_use(c, base, data, sym, line, depth + 1)
    elif t == "use_as_clause":
        path, alias = n.child_by_field_name("path"), n.child_by_field_name("alias")
        if path is not None and alias is not None:
            full = prefix + _text(path, data)
            head, _, last = full.rpartition("::")
            sym.add_import(Decl(_text(alias, data), "import", source=head, member=last, line=line))
    elif t == "use_wildcard":
        inner = next(iter(n.named_children), None)
        sym.wildcards.append(prefix + (_text(inner, data) if inner is not None else ""))
    elif t == "use_list":
        for c in n.named_children:
            _rust_use(c, prefix, data, sym, line, depth + 1)


_RUST_TYPES = {"struct_item": "struct", "enum_item": "enum", "type_item": "type", "trait_item": "trait", "union_item": "union"}


def _read_rust(root, data: bytes, sym: Symbols) -> None:
    for i, n in enumerate(root.named_children):
        if i >= MAX_TOP_NODES:
            break
        t = n.type
        if t == "use_declaration":
            arg = n.child_by_field_name("argument")
            if arg is not None:
                _rust_use(arg, "", data, sym, _line(n))
        elif t in ("const_item", "static_item"):
            name = n.child_by_field_name("name")
            if name is not None:
                sym.add_constant(Decl(_text(name, data), "constant" if t == "const_item" else "static", _flat(_text(n, data), TEXT_CHARS), _line(n)))
        elif t in _RUST_TYPES:
            name = n.child_by_field_name("name")
            if name is not None:
                sym.add_type(Decl(_text(name, data), _RUST_TYPES[t], _head(n, data, TYPE_CHARS) if t == "trait_item" else _flat(_text(n, data), TYPE_CHARS), _line(n)))
        elif t == "mod_item" and n.child_by_field_name("body") is None:
            name = n.child_by_field_name("name")
            if name is not None:
                sym.import_total += 1


def _c_declarator_name(node, data: bytes):
    found: list = []
    _collect(node, _C, found)
    return found[0] if found else None


def _read_c(root, data: bytes, sym: Symbols) -> None:
    def visit(nodes, depth: int) -> None:
        for n in nodes:
            t = n.type
            if t == "preproc_include":
                p = n.child_by_field_name("path")
                if p is not None:
                    sym.includes.append(_string_value(p, data).strip("<>") if _text(p, data).startswith("<") else _string_value(p, data))
            elif t in ("preproc_def", "preproc_function_def"):
                name = n.child_by_field_name("name")
                if name is not None:
                    sym.add_constant(Decl(_text(name, data), "macro", _flat(_text(n, data), TEXT_CHARS), _line(n)))
            elif t == "declaration":
                decls = _by_field(n, "declarator")
                quals = " ".join(_text(c, data) for c in n.named_children if c.type in ("type_qualifier",))
                for d in decls:
                    inner = d.child_by_field_name("declarator") if d.type == "init_declarator" else d
                    if inner is not None and inner.type == "function_declarator":
                        name = _c_declarator_name(inner, data)
                        if name is not None:
                            sym.protos.setdefault(_text(name, data), Decl(_text(name, data), "prototype", _flat(_text(n, data), TEXT_CHARS), _line(n)))
                        continue
                    name = _c_declarator_name(d, data)
                    if name is not None:
                        sym.add_constant(Decl(_text(name, data), "constant" if "const" in quals.split() else "variable", _flat(_text(n, data), TEXT_CHARS), _line(n)))
                ty = n.child_by_field_name("type")
                if ty is not None and ty.type in ("struct_specifier", "enum_specifier", "union_specifier"):
                    _c_type(ty, data, sym)
            elif t == "type_definition":
                name = n.child_by_field_name("declarator")
                if name is not None and name.type == "type_identifier":
                    sym.add_type(Decl(_text(name, data), "typedef", _flat(_text(n, data), TYPE_CHARS), _line(n)))
                ty = n.child_by_field_name("type")
                if ty is not None and ty.type in ("struct_specifier", "enum_specifier", "union_specifier"):
                    _c_type(ty, data, sym)
            elif t in ("struct_specifier", "enum_specifier", "union_specifier"):
                _c_type(n, data, sym)
            elif t in ("preproc_ifdef", "preproc_if", "preproc_else", "preproc_elif", "linkage_specification", "declaration_list") and depth < 4:
                visit(n.named_children, depth + 1)

    visit(root.named_children[:MAX_TOP_NODES], 0)


def _c_type(n, data: bytes, sym: Symbols) -> None:
    name = n.child_by_field_name("name")
    body = n.child_by_field_name("body")
    kind = n.type.replace("_specifier", "")
    if name is not None and body is not None:
        sym.add_type(Decl(_text(name, data), kind, _flat(_text(n, data), TYPE_CHARS), _line(n)))
    if n.type == "enum_specifier" and body is not None:
        for e in body.named_children:
            nm = e.child_by_field_name("name")
            if e.type == "enumerator" and nm is not None:
                sym.add_constant(Decl(_text(nm, data), "enumerator", _flat(_text(e, data), TEXT_CHARS), _line(e)))


_READERS = {"python": _read_python, "javascript": _read_js, "typescript": _read_js, "java": _read_java, "go": _read_go, "rust": _read_rust, "c": _read_c}


def _import_like(src: Source, root, data: bytes, sym: Symbols) -> None:
    """The text-only form: the top-level statements the splitter calls imports, and a module declaration."""
    for i, n in enumerate(root.named_children):
        if i >= MAX_TOP_NODES:
            break
        if src.kind_of(n.type) == "import":
            sym.import_total += 1
            if len(sym.import_text) < MAX_LISTED:
                sym.import_text.append(_flat(_text(n, data), IMPORT_TEXT_CHARS))
        elif n.type in ("package_clause", "package_declaration", "namespace_definition", "namespace_declaration", "file_scoped_namespace_declaration", "module") and not sym.module:
            sym.module = _head(n, data, 120)


def symbols_of(src: Source) -> Symbols:
    """The declarations of one version, read once and kept on the source."""
    if src.symbols is not None:
        return src.symbols
    resolved = src.lang in _READERS
    sym = Symbols("ast" if resolved else "text_only")
    if resolved:
        _READERS[src.lang](src.root, src.data, sym)
        sym.import_total += len(sym.import_order) + len(sym.wildcards)
        for key, info in src.units.info.items():
            if info.parent is None:
                sym.functions.setdefault((info.scope, info.name), []).append(info)
    else:
        _import_like(src, src.root, src.data, sym)
    src.symbols = sym
    return sym


def file_symbols(lang: str, path: str, text: str) -> Symbols | None:
    """The declarations of a file read on its own (the tests and tools use this; the splitter passes the trees it has already). ``None`` when there is no symbol reader for the language
    or no grammar."""
    if lang not in _READERS:
        return None
    from app.backend.pipeline import funcsplit
    try:
        units = funcsplit.parse_units(lang, path, text)
    except funcsplit.Unavailable:
        return None
    got = funcsplit._parse_for_edits(units.grammar or lang, text)
    if got is None:
        return None
    root, data = got
    return symbols_of(funcsplit.deps_source(lang, units, root, data))


# --- the references of one function ------------------------------------------------------------------------------------------------------------------------------------------

@dataclass
class Refs:
    imports: dict = field(default_factory=dict)         # name -> Decl
    constants: dict = field(default_factory=dict)
    types: dict = field(default_factory=dict)
    functions: dict = field(default_factory=dict)       # key -> UnitInfo
    protos: dict = field(default_factory=dict)
    unresolved: set = field(default_factory=set)
    truncated: bool = False


def _scan(node, data: bytes, rules: Rules) -> tuple[list[tuple[str, str]], set[str], bool]:
    """(uses, locals, truncated): every name the function mentions as (name, kind) with kind call, type or plain, and the names it binds itself."""
    uses: list[tuple[str, str]] = []
    bound_ids: set[int] = set()
    local: set[str] = set()
    cur = node.walk()
    stack: list = []
    fields: list = []
    seen = 0
    truncated = False
    while True:
        n = cur.node
        field_name = cur.field_name
        parent = stack[-1] if stack else None
        seen += 1
        prune = False
        if seen > MAX_SCAN_NODES:
            truncated = True
            break
        t = n.type
        if t in rules.prune:
            if rules is _PY and t in ("import_statement", "import_from_statement"):
                for d in _py_import_decls(n, data)[0]:
                    local.add(d.name)
            prune = True
        else:
            spec = rules.bind.get(t)
            if spec is not None:
                for fld in spec:
                    for child in (_spec_children(n, "*") if fld is None else _by_field(n, fld)):
                        found: list = []
                        _collect(child, rules, found)
                        for f in found:
                            bound_ids.add(f.id)
                            local.add(_text(f, data))
            if n.id not in bound_ids and (t in rules.ident or t in rules.types):
                ptype = parent.type if parent is not None else None
                if (ptype, field_name) not in rules.skip and (ptype, fields[-1] if fields else None) not in rules.skip_in:
                    name = _text(n, data)
                    kind = "plain"
                    if rules is _JAVA and ptype == "method_invocation" and field_name == "name":
                        kind = "call" if parent.child_by_field_name("object") is None else ""
                    elif (ptype, field_name) in rules.calls:
                        kind = "call"
                    elif t in rules.types or (name[:1].isupper() and not name.isupper()):
                        kind = "type"
                    if kind:
                        uses.append((name, kind))
        if not prune and cur.goto_first_child():
            stack.append(n)
            fields.append(field_name)
            continue
        done = False
        while not cur.goto_next_sibling():
            if not cur.goto_parent():
                done = True
                break
            stack.pop()
            fields.pop()
        if done:
            break
    if rules is _PY:
        glob = set()
        for g in _globals(node, data):
            glob.add(g)
        local -= glob
    return uses, local, truncated


def _globals(node, data: bytes) -> list[str]:
    """Names a Python function declares ``global``: they are assigned inside but belong to the module."""
    out = []
    stack = [node]
    seen = 0
    while stack and seen < MAX_SCAN_NODES:
        n = stack.pop()
        seen += 1
        if n.type == "global_statement":
            out.extend(_text(c, data) for c in n.named_children if c.type == "identifier")
            continue
        if n.type in ("function_definition", "class_definition") and n is not node:
            continue
        stack.extend(n.named_children)
    return out


_RANK = {"plain": 0, "type": 1, "call": 2}


def _scope_chain(scope: str) -> list[str]:
    parts = [p for p in scope.split(".") if p]
    return [".".join(parts[:i]) for i in range(len(parts), 0, -1)]


def references(src: Source, node, key: str) -> Refs:
    """Resolve the names the function ``key`` (syntax ``node``) mentions against the declarations of its file."""
    sym = symbols_of(src)
    rules = RULES[src.lang]
    info = src.units.info[key]
    uses, local, truncated = _scan(node, src.data, rules)
    # a function nested in another one sees what the outer function binds
    fn_types = _function_types(src)
    p = node.parent
    hops = 0
    while p is not None and hops < 30:
        if p.type in fn_types:
            local |= _scan(p, src.data, rules)[1]
        p = p.parent
        hops += 1
    local |= _outer_type_parameters(node, src)
    chain = _scope_chain(info.scope)
    enclosing_names = {c.split(".")[-1] for c in chain}
    refs = Refs(truncated=truncated)
    kinds: dict[str, str] = {}
    for name, kind in uses:                                     # a name used as a call anywhere is a call
        if _RANK[kind] > _RANK.get(kinds.get(name, ""), -1):
            kinds[name] = kind
    for name, kind in kinds.items():
        if name in local or name in enclosing_names:
            continue
        if name in sym.imports:
            refs.imports[name] = sym.imports[name]
            continue
        fns = [i for i in sym.functions.get(("", name), []) if i.key != key]
        if rules.bare_members:
            for c in chain:
                fns += [i for i in sym.functions.get((c, name), []) if i.key != key]
        if fns and (kind == "call" or name not in sym.constants):
            for i in fns[:3]:
                refs.functions[i.key] = i
            continue
        const = sym.constants.get(name)
        if const is None and rules.bare_members:
            for c in chain:
                const = sym.members.get(c, {}).get(name)
                if const is not None:
                    break
        if const is not None:
            refs.constants[name] = const
            continue
        if name in sym.types:
            refs.types[name] = sym.types[name]
            continue
        if name in sym.protos and kind != "type":
            refs.protos[name] = sym.protos[name]
            continue
        if kind in ("call", "type") and len(name) > 1 and name not in rules.builtins:        # a name of the file or an import comes before a builtin of the same name
            refs.unresolved.add(name)
    return refs


def _outer_type_parameters(node, src: Source) -> set[str]:
    """The type variables of the classes, impls and functions around a function (``class Box<T>``): they are names the function may use that are declared nowhere else."""
    out: set[str] = set()
    p = node.parent
    hops = 0
    while p is not None and hops < 40:
        if p.type in src.containers:
            tp = p.child_by_field_name("type_parameters")
            if tp is not None:
                for t in tp.named_children:
                    name = t.child_by_field_name("name") or next((c for c in t.named_children if c.type in ("type_identifier", "identifier")), None)
                    if name is not None:
                        out.add(_text(name, src.data))
        p = p.parent
        hops += 1
    return out


def _function_types(src: Source) -> frozenset:
    return frozenset({i.kind for i in src.units.info.values()})


# --- the context of a pair ---------------------------------------------------------------------------------------------------------------------------------------------------

_KIND_LABEL = (("class", "class"), ("interface", "interface"), ("trait", "trait"), ("impl", "impl"), ("struct", "struct"), ("enum", "enum"), ("namespace", "namespace"), ("module", "module"),
               ("mod_", "module"), ("object", "object"), ("record", "record"))


def _enclosing(src: Source, node) -> dict | None:
    p = node.parent
    hops = 0
    while p is not None and hops < 40:
        if p.type in src.containers:
            name = src.name_of(p, src.data) or ""
            t = p.type
            kind = next((label for word, label in _KIND_LABEL if word in t), t)
            return {"name": name, "kind": kind, "lines": [p.start_point[0] + 1, p.end_point[0] + 1], "header": _head(p, src.data, TYPE_CHARS)}
        p = p.parent
        hops += 1
    return None


def _import_entry(d: Decl) -> dict:
    e = {"name": d.name, "from": d.source, "line": d.line}
    if d.member and d.member != d.name:
        e["member"] = d.member
    return e


def _decl_entry(d: Decl) -> dict:
    return {"name": d.name, "kind": d.kind, "text": d.text, "line": d.line}


def _function_entry(i) -> dict:
    e = {"name": i.name, "key": i.key, "lines": list(i.lines), "signature": i.signature}
    if i.qual != i.name:
        e["qual"] = i.qual
    return e


def _cap(entries: list, limit: int, omitted: dict, label: str) -> list:
    if len(entries) > limit:
        omitted[label] = len(entries) - limit
    return entries[:limit]


def _same(a: Decl, b: Decl | None) -> bool:
    return b is not None and a.source == b.source and a.member == b.member


def context(before: Source, before_node, before_key: str, after: Source | None = None, after_node=None, after_key: str | None = None) -> dict:
    """The dependency context of one function pair (see the module docstring). Everything but ``delta`` is read from the version before the change."""
    sym = symbols_of(before)
    if sym.resolution != "ast":
        out = {"v": DEPS_VERSION, "resolution": "text_only", "note": TEXT_ONLY_HINT, "imports_total": sym.import_total, "import_statements": sym.import_text}
        if sym.module:
            out["module"] = sym.module
        enc = _enclosing(before, before_node)
        if enc:
            out["enclosing"] = enc
        return out
    refs = references(before, before_node, before_key)
    omitted: dict = {}
    by_line = lambda d: (d.line, d.name)           # noqa: E731
    imports = _cap([_import_entry(d) for d in sorted(refs.imports.values(), key=by_line)], MAX_LISTED, omitted, "imports")
    constants = _cap([_decl_entry(d) for d in sorted(refs.constants.values(), key=by_line)], MAX_CONSTANTS, omitted, "constants")
    types = _cap([_decl_entry(d) for d in sorted(refs.types.values(), key=by_line)], MAX_TYPES, omitted, "types")
    functions = [_function_entry(i) for i in sorted(refs.functions.values(), key=lambda i: (i.lines[0], i.key))]
    for d in sorted(refs.protos.values(), key=by_line):
        functions.append({"name": d.name, "lines": [d.line, d.line], "prototype": True, "text": d.text})
    functions = _cap(functions, MAX_FUNCTIONS, omitted, "functions")
    out: dict = {"v": DEPS_VERSION, "resolution": "ast", "imports": imports, "imports_total": sym.import_total, "constants": constants, "types": types, "functions": functions,
                 "unresolved": _cap(sorted(refs.unresolved), MAX_LISTED, omitted, "unresolved")}
    if sym.module:
        out["module"] = sym.module
    if sym.includes:
        out["includes"] = sym.includes[:MAX_LISTED]
        if len(sym.includes) > MAX_LISTED:
            omitted["includes"] = len(sym.includes) - MAX_LISTED
    if sym.wildcards:
        out["wildcards"] = _cap(list(dict.fromkeys(sym.wildcards)), MAX_LISTED, omitted, "wildcards")
    enc = _enclosing(before, before_node)
    if enc:
        out["enclosing"] = enc
    if refs.truncated:
        out["truncated"] = True
    out["delta"] = _delta(before, refs, after, after_node, after_key, omitted) if after is not None and after_node is not None and after_key else {}
    if omitted:
        out["omitted"] = omitted
    return out


def _delta(before: Source, refs: Refs, after: Source, after_node, after_key: str, omitted: dict) -> dict:
    """What the fix changes about the function's dependencies (the one place the fixed version speaks)."""
    bs, as_ = symbols_of(before), symbols_of(after)
    ar = references(after, after_node, after_key)
    added = [_import_entry(d) for n, d in sorted(ar.imports.items(), key=lambda kv: (kv[1].line, kv[0])) if not _same(d, bs.imports.get(n))]
    removed = [_import_entry(d) for n, d in sorted(refs.imports.items(), key=lambda kv: (kv[1].line, kv[0])) if not _same(d, as_.imports.get(n))]
    changed = []
    for n in sorted(set(refs.constants) | set(ar.constants)):
        b, a = bs.constants.get(n) or refs.constants.get(n), as_.constants.get(n) or ar.constants.get(n)
        if b is not None and a is not None and b.text != a.text:
            changed.append({"name": n, "before": b.text, "after": a.text})

    def names(r: Refs) -> dict[str, str]:
        out = {n: "import" for n in r.imports}
        out.update({n: "constant" for n in r.constants})
        out.update({n: "type" for n in r.types})
        out.update({i.name: "function" for i in r.functions.values()})
        out.update({n: "function" for n in r.protos})
        return out

    nb, na = names(refs), names(ar)
    return {"imports_added": _cap(added, MAX_LISTED, omitted, "imports_added"), "imports_removed": _cap(removed, MAX_LISTED, omitted, "imports_removed"),
            "constants_changed": _cap(changed, MAX_CONSTANTS, omitted, "constants_changed"),
            "references_added": _cap([{"name": n, "kind": k} for n, k in sorted(na.items()) if n not in nb], MAX_LISTED, omitted, "references_added"),
            "references_removed": _cap([{"name": n, "kind": k} for n, k in sorted(nb.items()) if n not in na], MAX_LISTED, omitted, "references_removed")}
