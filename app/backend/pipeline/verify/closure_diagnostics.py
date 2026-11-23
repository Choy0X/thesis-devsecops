"""Lexical missing-fact diagnosis, deliberately separate from proof grammar.

Knowing that a name is bound is not knowing that its implementation is safe.
These routines only avoid inventing source gaps for parameters, property names
and runtime-provided names. They never admit or execute a security example.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import pathlib

from .. import evidence_ast

VERSION = 'mechanism-closure-diagnostics/5'
MAX_BINDING_NODES = 100000
MAX_BINDING_REFERENCES = 25000


class BindingAnalysisCancelled(RuntimeError):
    pass


class _BindingLimit(RuntimeError):
    pass
# Standard runtime names are diagnosis-only. Do not reuse this set in the
# restricted executor or source-rule validators.
ECMASCRIPT = frozenset('''Object Function Boolean Symbol Number BigInt Math Date
String RegExp Array Int8Array Uint8Array Uint8ClampedArray Int16Array Uint16Array
Int32Array Uint32Array Float32Array Float64Array BigInt64Array BigUint64Array
Map Set WeakMap WeakSet ArrayBuffer SharedArrayBuffer DataView Atomics JSON
Promise Reflect Proxy Intl Error AggregateError EvalError RangeError
ReferenceError SyntaxError TypeError URIError undefined NaN Infinity globalThis
parseInt parseFloat isNaN isFinite decodeURI decodeURIComponent encodeURI
encodeURIComponent eval'''.split())
NODE = frozenset(('process', 'Buffer', 'console', 'global', 'setImmediate',
                  'clearImmediate', 'setTimeout', 'clearTimeout', 'setInterval',
                  'clearInterval', 'queueMicrotask', 'URL', 'URLSearchParams',
                  'TextEncoder', 'TextDecoder'))
COMMONJS = frozenset(('require', 'module', 'exports', '__dirname', '__filename'))
NODE_MODULES = frozenset(('path', 'child_process', 'fs', 'fs/promises', 'util',
                          'os', 'url', 'buffer', 'events', 'stream', 'assert',
                          'crypto', 'http', 'https', 'net', 'tls', 'zlib'))
FUNCTIONS = frozenset(('function_declaration', 'function_expression',
                       'generator_function_declaration', 'generator_function',
                       'arrow_function', 'method_definition'))
TYPE_ONLY = frozenset(('type_annotation', 'type_arguments', 'type_parameters',
                       'interface_declaration', 'type_alias_declaration',
                       'type_query', 'implements_clause'))


@dataclass
class Scope:
    parent: Scope | None = None
    function: bool = False
    names: set[str] = field(default_factory=set)
    declarations: dict = field(default_factory=dict)
    span: tuple | None = None

    def contains(self, name):
        return name in self.names or self.parent is not None and self.parent.contains(name)

    def variable_scope(self):
        scope = self
        while scope.parent is not None and not scope.function:
            scope = scope.parent
        return scope


def free_values(source, language='javascript'):
    """Unbound runtime value references, with genuine nested lexical scopes."""
    scanned = _binding_scan(source, language)
    if scanned is None:
        return None
    declarations, references, _, _ = scanned
    return {name for name, scope, node in references
            if node.id not in declarations and not scope.contains(name)}


def _binding_scan(source, language, *, full_module=False, cancelled=None):
    if cancelled and cancelled():
        raise BindingAnalysisCancelled('Binding analysis cancelled')
    parse = evidence_ast.parse_snapshot if full_module else evidence_ast.parse
    parsed = parse(language, source)
    if parsed is None:
        parsed = None if full_module else parse(language, 'class Subject {\n' + source + '\n}')
    if parsed is None:
        return None
    tree, raw = parsed
    declarations, references = set(), []
    unsupported = [False]
    visited = [0]
    root = Scope(function=True, span=(0, len(raw)))

    def text(node):
        return raw[node.start_byte:node.end_byte].decode('utf-8') if node is not None else ''

    def declare(node, scope):
        if node is None:
            return
        if node.type in ('identifier', 'type_identifier', 'shorthand_property_identifier_pattern'):
            if '\\' in text(node):
                unsupported[0] = True
            scope.names.add(text(node)); declarations.add(node.id)
            scope.declarations.setdefault(text(node), []).append(node)
        elif node.type in ('required_parameter', 'optional_parameter'):
            declare(node.child_by_field_name('pattern'), scope)
        elif node.type in ('assignment_pattern', 'object_assignment_pattern'):
            declare(node.child_by_field_name('left'), scope)
        elif node.type == 'pair_pattern':
            declare(node.child_by_field_name('value'), scope)
        elif node.type in ('formal_parameters', 'object_pattern', 'array_pattern', 'rest_pattern'):
            for child in node.named_children:
                declare(child, scope)

    def walk(node, scope):
        visited[0] += 1
        if visited[0] > MAX_BINDING_NODES or len(references) > MAX_BINDING_REFERENCES:
            raise _BindingLimit()
        if visited[0] % 256 == 0 and cancelled and cancelled():
            raise BindingAnalysisCancelled('Binding analysis cancelled')
        kind = node.type
        if kind == 'with_statement':
            unsupported[0] = True
        if kind in TYPE_ONLY:
            return
        if kind in FUNCTIONS:
            name = node.child_by_field_name('name')
            if kind in ('function_declaration', 'generator_function_declaration'):
                declare(name, scope)
            local = Scope(scope, function=True, span=(node.start_byte, node.end_byte))
            if kind not in ('arrow_function', 'method_definition', 'function_declaration', 'generator_function_declaration'):
                declare(name, local)
            if kind != 'arrow_function':
                local.names.add('arguments')
            declare(node.child_by_field_name('parameters'), local)
            declare(node.child_by_field_name('parameter'), local)
            for child in node.named_children:
                walk(child, local)
            return
        if kind == 'class_declaration':
            declare(node.child_by_field_name('name'), scope)
            scope = Scope(scope, span=(node.start_byte, node.end_byte))
        elif kind in ('statement_block', 'switch_body', 'for_statement', 'for_in_statement', 'catch_clause'):
            scope = Scope(scope, span=(node.start_byte, node.end_byte))
            if kind == 'catch_clause':
                declare(node.child_by_field_name('parameter'), scope)
            elif kind == 'for_in_statement':
                keyword = next((child.type for child in node.children if child.type in ('let', 'const', 'var')), None)
                if keyword:
                    declare(node.child_by_field_name('left'), scope.variable_scope() if keyword == 'var' else scope)
        if kind == 'variable_declarator':
            parent = node.parent
            target = scope.variable_scope() if parent is not None and parent.type == 'variable_declaration' else scope
            declare(node.child_by_field_name('name'), target)
        elif kind == 'import_statement':
            # Imports establish bindings; their semantic origin is checked by
            # mechanism validators, not assumed here.
            stack = list(node.named_children)
            while stack:
                child = stack.pop()
                if child.type == 'import_specifier':
                    declare(child.child_by_field_name('alias') or child.child_by_field_name('name'), scope)
                elif child.type == 'identifier':
                    declare(child, scope)
                elif child.type != 'string':
                    stack.extend(child.named_children)
            return
        if kind in ('identifier', 'shorthand_property_identifier', 'shorthand_property_identifier_pattern'):
            if '\\' in text(node) or text(node) in ('eval', 'Function'):
                unsupported[0] = True
            parent = node.parent
            # A non-computed key is a name, not an evaluated value. Computed
            # keys are represented by a separate computed_property_name node.
            if parent is None or parent.type != 'pair' or parent.child_by_field_name('key') != node:
                references.append((text(node), scope, node))
        for child in node.named_children:
            walk(child, scope)

    try:
        walk(tree, root)
    except (RecursionError, _BindingLimit):
        return None
    if unsupported[0]:
        return None
    return declarations, references, root, raw


def _reference_effect(node, raw):
    """Classify one reference once, including non-expression write targets."""
    def target_kind(left):
        if left is None or not left.start_byte <= node.start_byte < node.end_byte <= left.end_byte:
            return None
        cursor = node
        mutation = False
        while cursor != left:
            parent = cursor.parent
            if parent is None or parent.type == 'computed_property_name':
                return None
            if parent.type in ('assignment_pattern', 'object_assignment_pattern'):
                target = parent.child_by_field_name('left')
                if target is None or not target.start_byte <= node.start_byte < node.end_byte <= target.end_byte:
                    return None
            if parent.type in ('member_expression', 'subscript_expression'):
                obj = parent.child_by_field_name('object')
                if obj is None or not obj.start_byte <= node.start_byte < node.end_byte <= obj.end_byte:
                    return None
                mutation = True
            cursor = parent
        return 'object_mutated' if mutation else 'binding_reassigned'
    kind, cursor = None, node.parent
    while cursor is not None:
        if cursor.type in ('assignment_expression', 'augmented_assignment_expression', 'update_expression', 'for_in_statement'):
            kind = target_kind(cursor.child_by_field_name('left') or cursor.child_by_field_name('argument'))
            break
        if cursor.type == 'unary_expression' and any(child.type == 'delete' for child in cursor.children):
            kind = target_kind(cursor.child_by_field_name('argument'))
            break
        if cursor.type in FUNCTIONS:
            break
        cursor = cursor.parent
    cursor = node
    escaped = False
    while cursor.parent is not None:
        parent = cursor.parent
        if parent.type == 'binary_expression':
            operator = parent.child_by_field_name('operator')
            operation = operator.type if operator is not None else None
            if operation in ('===', '!=='):
                break
            if operation == 'instanceof':
                # The left value is passed to the right value's possibly custom
                # Symbol.hasInstance. The constructor reference itself is not
                # returned by this boolean expression.
                escaped = parent.child_by_field_name('left') == cursor
                break
        if parent.type == 'unary_expression':
            operator = parent.child_by_field_name('operator')
            if operator is not None and operator.type in ('typeof', '!', 'void'):
                break
        if parent.type == 'ternary_expression' and parent.child_by_field_name('condition') == cursor:
            break
        if parent.type == 'call_expression' and parent.child_by_field_name('function') == cursor:
            break
        if parent.type == 'new_expression' and parent.child_by_field_name('constructor') == cursor:
            break
        if parent.type == 'arguments':
            escaped = True
            break
        if parent.type == 'template_substitution':
            template = parent.parent
            call = template.parent if template is not None else None
            escaped = (call is not None and call.type == 'call_expression'
                       and call.child_by_field_name('arguments') == template)
            break
        value_field = ('value' if parent.type == 'variable_declarator' else
                       'right' if parent.type in ('assignment_expression', 'augmented_assignment_expression',
                                                  'assignment_pattern', 'object_assignment_pattern') else None)
        if value_field and parent.child_by_field_name(value_field) == cursor:
            escaped = cursor.type != 'member_expression' or raw[
                cursor.child_by_field_name('property').start_byte:cursor.child_by_field_name('property').end_byte] == b'prototype'
            break
        if (parent.type in ('return_statement', 'throw_statement', 'yield_expression')
                or parent.type == 'arrow_function' and parent.child_by_field_name('body') == cursor):
            escaped = True
            break
        if parent.type in FUNCTIONS:
            break
        cursor = parent
    return kind, escaped


def binding_origins(source, span, language, names, *, cancelled=None):
    """Describe target runtime bindings and writes without granting a proof.

    Function declarations recurse through the enclosing mutable binding. Named
    function expressions instead have their internal name binding. A same-name
    write in another lexical scope cannot rebind the consumed declaration.
    """
    scanned = _binding_scan(source, language, full_module=True, cancelled=cancelled)
    if scanned is None:
        return None
    declarations, references, module, raw = scanned
    if (not isinstance(span, (list, tuple)) or len(span) != 2
            or any(type(value) is not int for value in span) or not 0 <= span[0] <= span[1] <= len(raw)):
        raise ValueError('Binding origins require an exact bounded byte span.')
    owners = {}
    def owner(scope, name):
        key = (id(scope), name)
        if key in owners:
            return owners[key]
        while scope is not None and name not in scope.names:
            scope = scope.parent
        owners[key] = scope
        return scope
    effects = {}
    for number, (name, scope, node) in enumerate(references):
        if number % 256 == 0 and cancelled and cancelled():
            raise BindingAnalysisCancelled('Binding analysis cancelled')
        if name not in names or node.id in declarations:
            continue
        key = (name, id(owner(scope, name)))
        item = effects.setdefault(key, {'binding_reassigned':False, 'object_mutated':False,
                                       'escaped':False, 'writes':[], 'escape_spans':[]})
        kind, escaped = _reference_effect(node, raw)
        if kind:
            item[kind] = True
            item['writes'].append({'kind':kind, 'span':[node.start_byte, node.end_byte]})
        if escaped:
            item['escaped'] = True
            item['escape_spans'].append([node.start_byte, node.end_byte])
    facts = {}
    results = {}
    for name, scope, node in references:
        if name not in names or node.id in declarations or node.start_byte < span[0] or node.end_byte > span[1]:
            continue
        bound = owner(scope, name)
        key = (name, id(bound))
        if key in facts:
            current = facts[key]
            if name in results and results[name] != current:
                return None
            results[name] = current
            continue
        origin = ('global' if bound is None else 'module' if bound is module else
                  'local' if bound.span is not None and span[0] <= bound.span[0] and bound.span[1] <= span[1] else 'enclosing')
        definitions = [] if bound is None else bound.declarations.get(name, [])
        declaration_facts = [{'text': raw[n.start_byte:n.end_byte].decode('utf-8'), 'span': [n.start_byte, n.end_byte],
                              'parent_kind': n.parent.type if n.parent is not None else None,
                              'uninitialized': n.parent is not None and n.parent.type == 'variable_declarator'
                                  and n.parent.child_by_field_name('value') is None} for n in definitions]
        current = {'origin': origin, 'declarations': declaration_facts, **effects[key]}
        current['binding_reassigned'] |= len(definitions) > 1
        facts[key] = current
        if name in results and results[name] != current:
            return None  # Multiple target scopes resolve this spelling differently.
        results[name] = current
    return results


def _object(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _environment(record, side, imports):
    """Runtime anchors for diagnosis; npm alone does not prove CommonJS."""
    path = record.get('file_path_' + side) or ''
    suffix = pathlib.PurePosixPath(path).suffix.lower()
    node_import = any(binding and binding[0] in NODE_MODULES
                      for binding in imports.values())
    commonjs = suffix == '.cjs'
    node = commonjs or node_import
    context = _object(record.get('context_' + side))
    files = (context.get('snapshot') or {}).get('files') or {}
    for item in context.get('items', []):
        if not isinstance(item, dict) or item.get('kind') != 'runtime_environment':
            continue
        import hashlib
        text = item.get('text')
        if not isinstance(text, str) or files.get(item.get('path')) != hashlib.sha256(text.encode()).hexdigest():
            continue
        declaration = _object(text)
        engines = declaration.get('engines') or {}
        declared_node = (isinstance(engines, dict) and bool(engines.get('node'))
                         or declaration.get('type') in ('commonjs', 'module')
                         or bool(declaration.get('main') or declaration.get('bin') or declaration.get('exports')))
        if declared_node:
            node = True
            commonjs = commonjs or suffix in ('.js', '.ts', '.cts') and declaration.get('type') != 'module'
    for item in context.get('items', []):
        if not isinstance(item, dict) or item.get('path') != path or item.get('kind') != 'imports':
            continue
        source = item.get('text', '')
        # A supplied builtin require binding in this .js unit anchors CommonJS;
        # require in an ES module is not magically supplied by Node.
        if node_import and suffix in ('.js', '.ts', '.cts') and 'require' in source and 'import ' not in source:
            commonjs = True
    return ECMASCRIPT | (NODE if node else frozenset()) | (COMMONJS if commonjs else frozenset())


def missing_values(record, side, *, imports, mechanism_names, module):
    """Diagnose only actual free values and reachable supplied dependencies."""
    context = _object(record.get('context_' + side))
    items = context.get('items', [])
    definitions = {}
    for item in items:
        if not isinstance(item, dict) or item.get('kind') not in ('module_symbol', 'callee') or not item.get('name'):
            continue
        name, value = item['name'], item.get('text', '')
        definitions.setdefault(name, set()).add(value)
    language = 'typescript' if record.get('language') in ('typescript', 'ts') else 'javascript'
    source = record.get('vulnerable_code' if side == 'before' else 'fixed_code') or ''
    pending = list(free_values(source, language) or ())
    lexical = {}
    for item in items:
        if not isinstance(item, dict) or item.get('kind') != 'lexical_scope' or item.get('path') != record.get('file_path_' + side):
            continue
        import hashlib
        from ..evidence_source_scope import lexical_item
        text = item.get('text')
        if not isinstance(text, str) or item.get('target_sha256') != hashlib.sha256(source.encode()).hexdigest():
            continue
        # Re-derive the lexical relationship; metadata cannot declare arbitrary
        # helper names to be bound. This is diagnosis only, never a proof.
        resolved = lexical_item(text, source, item['path'], language, item.get('name'))
        if resolved:
            lexical[item['name']] = free_values(text, language) or set()
    seen, gaps = set(), []
    environment = _environment(record, side, imports)
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        if name in mechanism_names:
            if name not in imports:
                gaps.append(f'{side}:binding:{name}:{module}')
            # Known nonbuiltin or ambiguous bindings are unsupported, not
            # absent. The runtime planner/source rules still reject them.
            continue
        values = definitions.get(name) or set()
        if len(values) == 1:
            value = next(iter(values))
            free = free_values(value, language)
            if free is not None:
                pending.extend(free - {name})
            continue
        if name in environment:
            continue
        if name in lexical:
            pending.extend(lexical[name])
            continue
        if imports.get(name) and imports[name][0] in NODE_MODULES:
            continue
        if name in COMMONJS | NODE:
            gaps.append(f'{side}:environment:{name}')
        elif name not in imports or name not in {'path', 'join', 'resolve', 'relative', 'isAbsolute', 'normalize', 'sep'}:
            gaps.append(f'{side}:definition:{name}')
    return sorted(set(gaps))
