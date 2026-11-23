"""Authenticate lexical dependencies of a previously checked source rule.

This layer proves only that references consume the expected immutable snapshot
definitions. The mechanism validator still has to establish security semantics.
Full modules are transient proof inputs and never expand the student context.
"""
from __future__ import annotations

import hashlib
import json
import re

from . import evidence_ast, evidence_module_witness
from .verify.closure_diagnostics import binding_origins, free_values

SCHEMA = 'source-binding-closure/1'
_NODE_EXPORTS = {
    'child_process': ('namespace', 'exec', 'execSync', 'execFile', 'execFileSync', 'spawn', 'spawnSync'),
    'path': ('namespace', 'resolve', 'relative', 'isAbsolute', 'normalize', 'join', 'dirname', 'basename', 'sep'),
    'util': ('promisify',), 'events': ('EventEmitter',)}
_INTRINSICS = frozenset(('Object', 'Array', 'Buffer', 'Date', 'RegExp', 'JSON', 'Math',
                       'String', 'Number', 'Boolean', 'Symbol', 'Error', 'TypeError', 'Reflect',
                       'parseInt', 'parseFloat', 'isNaN', 'isFinite', 'decodeURI', 'decodeURIComponent',
                       'encodeURI', 'encodeURIComponent', 'Infinity', 'NaN', 'undefined', 'Promise', 'process'))


def _context(record, side):
    value = record.get('context_' + side) or {}
    try:
        return json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return None


def _self_name(code, language):
    parsed = evidence_ast.parse(language, code)
    if parsed is None:
        return None
    tree, raw = parsed
    pending = [tree]
    while pending:
        node = pending.pop()
        if node.type in ('function_declaration', 'function_expression', 'arrow_function'):
            name = node.child_by_field_name('name')
            if name is None and node.parent is not None:
                parent = node.parent
                name = (parent.child_by_field_name('name') if parent.type == 'variable_declarator' else
                        parent.child_by_field_name('left') if parent.type == 'assignment_expression' else None)
            return raw[name.start_byte:name.end_byte].decode() if name is not None and name.type == 'identifier' else None
        pending.extend(reversed(node.named_children))
    return None


def _cjs_envelope_only(code, language, name):
    """The extraction envelope is not a dependency consumed by its function."""
    from .evidence_rules import children, text
    parsed = evidence_ast.parse(language, code)
    if parsed is None:
        return False
    tree, raw = parsed
    nodes = children(tree)
    if len(nodes) != 1 or nodes[0].type != 'expression_statement' or len(children(nodes[0])) != 1:
        return False
    assignment = children(nodes[0])[0]
    if assignment.type != 'assignment_expression':
        return False
    left, right = assignment.child_by_field_name('left'), assignment.child_by_field_name('right')
    permitted = text(left, raw) == 'module.exports' if name == 'module' else bool(
        re.fullmatch(r'exports\.(?!__proto__$|prototype$|constructor$)[A-Za-z_$][A-Za-z0-9_$]*', text(left, raw)))
    free = free_values(text(right, raw), language) if right is not None else None
    return permitted and right.type in ('function_expression', 'arrow_function') and free is not None and name not in free


def _declarative_initialization(source, language):
    """Reject unanalysed module execution; declarations do not run their bodies.

    Imports remain dependency facts for the mechanism validator, not an assertion
    that arbitrary imported code is harmless. No invocation, reflected evaluator,
    property write or control-flow initializer can hide behind a helper snippet.
    """
    parsed = evidence_ast.parse_snapshot(language, source)
    if parsed is None:
        return False
    tree, raw = parsed
    def children(node):
        return [n for n in node.named_children if n.type != 'comment']
    def text(node):
        return raw[node.start_byte:node.end_byte].decode() if node is not None else ''
    def readonly_native(node, expected, module=None):
        if node is None or node.type != 'identifier':
            return False
        name = text(node)
        facts = binding_origins(source, [node.start_byte, node.end_byte], language, {name})
        fact = (facts or {}).get(name)
        if not fact or any(fact[key] for key in ('binding_reassigned', 'object_mutated', 'escaped')):
            return False
        if module is None:
            return name == expected and fact['origin'] == 'global'
        from . import evidence_shell
        row = {'language': language, 'file_path_before': 'module.js',
               'context_before': {'items': [{'kind': 'imports', 'path': 'module.js', 'text': source}]}}
        return fact['origin'] == 'module' and evidence_shell._binding(row, 'before', name, expected, module=module)
    def pure(node):
        if node is None:
            return True
        if node.type in ('identifier', 'number', 'string', 'true', 'false', 'null', 'regex',
                         'function_expression', 'arrow_function', 'function_declaration'):
            return True
        if node.type == 'parenthesized_expression':
            return all(pure(child) for child in children(node))
        if node.type == 'array':
            return all(pure(child) and child.type != 'spread_element' for child in children(node))
        if node.type == 'object':
            return all(child.type == 'pair' and child.child_by_field_name('key').type != 'computed_property_name'
                       and pure(child.child_by_field_name('value')) for child in children(node))
        if node.type == 'member_expression':
            base = node
            while base.type == 'member_expression':
                base = base.child_by_field_name('object')
            return base.type == 'identifier' and text(base) in _INTRINSICS
        if node.type == 'call_expression':
            args = node.child_by_field_name('arguments')
            if text(node.child_by_field_name('function')) == 'promisify' and args is not None and len(children(args)) == 1:
                from . import evidence_shell
                argument = children(args)[0]
                if argument.type != 'identifier':
                    return False
                local = text(argument)
                row = {'language':language, 'file_path_before':'module.js',
                       'context_before':{'items':[{'kind':'imports', 'path':'module.js', 'text':source}]}}
                origins = binding_origins(source, [0,len(source.encode())], language, {'promisify', local})
                return (origins is not None and set(origins) == {'promisify', local}
                    and all(fact['origin'] == 'module' and not fact['binding_reassigned'] and not fact['object_mutated']
                            for fact in origins.values())
                    and evidence_shell._binding(row,'before','promisify','promisify',module='util')
                    and any(evidence_shell._binding(row,'before',local,method) for method in ('exec','execFile')))
            return (text(node.child_by_field_name('function')) == 'require' and args is not None
                    and len(children(args)) == 1 and children(args)[0].type == 'string')
        if node.type == 'new_expression':
            args = node.child_by_field_name('arguments')
            values = children(args) if args is not None else []
            return (readonly_native(node.child_by_field_name('constructor'), 'Set') and len(values) <= 1
                    and (not values or values[0].type == 'array' and all(child.type in
                         ('string', 'number', 'true', 'false', 'null') for child in children(values[0]))))
        return False
    def declarator(node):
        if node.type != 'variable_declarator':
            return False
        name, value = node.child_by_field_name('name'), node.child_by_field_name('value')
        if name is None or not pure(value):
            return False
        if name.type == 'identifier':
            return True
        # Destructuring can invoke arbitrary computed keys, defaults, getters,
        # proxies and iterators. Only exact ordinary Node builtin exports have
        # an independently known inert property-read contract here.
        if name.type != 'object_pattern':
            return False
        fields = []
        for child in children(name):
            if child.type == 'shorthand_property_identifier_pattern':
                fields.append((text(child), text(child)))
            elif child.type == 'pair_pattern':
                key, target = child.child_by_field_name('key'), child.child_by_field_name('value')
                if key is None or target is None or key.type != 'property_identifier' or target.type != 'identifier':
                    return False
                fields.append((text(key), text(target)))
            else:
                return False
        from . import evidence_shell
        row = {'language': language, 'file_path_before': 'module.js',
               'context_before': {'items': [{'kind': 'imports', 'path': 'module.js', 'text': source}]}}
        return bool(fields) and all(any(exported in exports and exported != 'namespace'
            and evidence_shell._binding(row, 'before', local, exported, module=module)
            for module, exports in _NODE_EXPORTS.items()) for exported, local in fields)

    def declaration(node):
        if node.type in ('function_declaration', 'generator_function_declaration', 'interface_declaration',
                         'type_alias_declaration', 'import_statement', 'empty_statement'):
            return True
        if node.type == 'class_declaration':
            # Methods and ordinary instance initializers are deferred. Computed
            # keys, decorators, static execution and unknown heritage are not.
            body = node.child_by_field_name('body')
            allowed = {'type_identifier', 'identifier', 'class_body', 'class_heritage'}
            if body is None or any(child.type not in allowed for child in children(node)):
                return False
            for heritage in (child for child in children(node) if child.type == 'class_heritage'):
                bases = children(heritage)
                if len(bases) == 1 and bases[0].type == 'extends_clause':
                    bases = children(bases[0])
                if len(bases) != 1 or not (readonly_native(bases[0], 'Error') or
                        readonly_native(bases[0], 'TypeError') or readonly_native(bases[0], 'EventEmitter', 'events')):
                    return False
            for member in children(body):
                name = member.child_by_field_name('name') or member.child_by_field_name('property')
                if (name is None or name.type not in ('property_identifier', 'private_property_identifier', 'string', 'number')
                        or any(child.type == 'decorator' for child in children(member))):
                    return False
                if member.type == 'method_definition':
                    continue
                if member.type not in ('public_field_definition', 'field_definition') or any(child.type == 'static' for child in member.children):
                    return False
            return True
        if node.type == 'export_statement':
            value = node.child_by_field_name('declaration')
            return declaration(value) if value is not None else pure(node.child_by_field_name('value'))
        if node.type in ('lexical_declaration', 'variable_declaration'):
            return all(declarator(child) for child in children(node))
        if node.type == 'expression_statement' and len(children(node)) == 1:
            value = children(node)[0]
            if value.type == 'string':
                return True
            if value.type == 'assignment_expression':
                left, right = value.child_by_field_name('left'), value.child_by_field_name('right')
                if left is None or right is None:
                    return False
                # Consumed identifier rebinding is independently rejected by
                # binding_origins; an initialization to a function can be pure.
                if left.type == 'identifier':
                    return right.type in ('function_expression', 'arrow_function')
                exported = re.fullmatch(r'exports\.([A-Za-z_$][A-Za-z0-9_$]*)', text(left))
                if not (text(left) == 'module.exports' or exported and exported[1] not in ('__proto__', 'prototype', 'constructor')):
                    return False
                namespace = 'module' if text(left) == 'module.exports' else 'exports'
                bound = binding_origins(source, [node.start_byte, node.end_byte], language, {namespace})
                return (bound is not None and namespace in bound and bound[namespace]['origin'] == 'global'
                        and not bound[namespace]['binding_reassigned'] and pure(right))
        return False
    return all(declaration(node) for node in children(tree))


def verify(record, side, helpers, *, provider=None, validated_rule=False):
    from . import evidence_recursive, evidence_rules
    def fail(cause, **details):
        return {'schema': SCHEMA, 'valid': False, 'cause': cause, 'security_verdict': None, **details}
    context = _context(record, side)
    if not isinstance(context, dict) or not isinstance(helpers, list) or len(helpers) > 16:
        return fail('invalid_binding_closure_request')
    if len({item.get('name') for item in helpers}) != len(helpers):
        return fail('ambiguous_helper_binding')
    main = evidence_module_witness.resolve(record, side, provider=provider)
    if main['state'] != 'available':
        return fail(main['cause'])
    witnesses = [(None, main)]
    definitions = {}
    for helper in helpers:
        matches = [item for item in context.get('items', []) if item.get('kind') in ('callee', 'module_symbol')
                   and item.get('name') == helper.get('name') and item.get('path') == helper.get('path')]
        if not matches or len({item.get('text') for item in matches}) != 1:
            return fail('selected_helper_evidence_missing_or_ambiguous')
        text = matches[0].get('text')
        parsed = evidence_ast.parse(record.get('language'), text)
        if parsed is None or evidence_rules._template_bindings(parsed, helper.get('text'), record.get('language'), set()) is None:
            return fail('selected_helper_semantics_mismatch')
        path = helper['path']
        source_hash = (context.get('snapshot') or {}).get('files', {}).get(path)
        helper_context = {'snapshot': {'revision': record.get('relevant_parent_commit' if side == 'before' else 'fix_commit'),
            'side': side, 'path': path, 'target_sha256': hashlib.sha256(text.encode()).hexdigest(),
            **({'file_sha256': source_hash} if source_hash else {})}}
        subject = {**record, 'file_path_' + side: path,
            ('vulnerable_code' if side == 'before' else 'fixed_code'): text,
            'context_' + side: helper_context}
        subject.pop('byte_span_' + side, None)
        witness = evidence_module_witness.resolve(subject, side, provider=provider, target_kind='declaration')
        if witness['state'] != 'available':
            return fail('helper_' + witness['cause'])
        definitions[helper['name']] = witness['binding']
        witnesses.append((helper['name'], witness))
    coherent_modules = {}
    for _, witness in witnesses:
        binding = witness['binding']
        identity = tuple(binding[name] for name in ('repository', 'revision', 'side', 'path'))
        content = tuple(binding[name] for name in ('module_sha256', 'grammar'))
        if identity in coherent_modules and coherent_modules[identity] != content:
            return fail('inconsistent_module_witness', binding_path=binding['path'])
        coherent_modules[identity] = content
    modules = {w['binding']['path']: {'kind': 'module_scope', 'path': w['binding']['path'],
               'text': w['module_text']} for _, w in witnesses}
    consumed = [(w['binding']['path'], w['binding']['byte_span']) for _, w in witnesses]
    for helper_name, witness in witnesses:
        binding = witness['binding']
        code = record.get('vulnerable_code' if side == 'before' else 'fixed_code') if helper_name is None else next(
            item['text'] for item in context.get('items', []) if item.get('name') == helper_name and item.get('path') == binding['path'])
        self_name = _self_name(code, record.get('language'))
        free = free_values(code, record.get('language'))
        if free is None:
            return fail('unit_binding_scope_unsupported')
        names = set(definitions) | _INTRINSICS | set(free) | ({self_name} if self_name else set())
        origins = binding_origins(witness['module_text'], binding['byte_span'], record.get('language'), names)
        if origins is None:
            return fail('binding_scope_unsupported')
        ambient = binding_origins(witness['module_text'], [0, len(witness['module_text'].encode())],
                                  record.get('language'), {'globalThis', 'global', 'window', 'self'})
        if ambient is None or any(fact['origin'] == 'global' for fact in ambient.values()):
            return fail('ambient_global_access_unsupported')
        loader = binding_origins(witness['module_text'], [0, len(witness['module_text'].encode())],
                                 record.get('language'), {'require'})
        if loader is None or any(fact['origin'] != 'global' or fact['binding_reassigned']
                                 or fact['object_mutated'] for fact in loader.values()):
            return fail('commonjs_loader_binding_unsupported')
        for name, fact in origins.items():
            envelope_start = binding['byte_span'][0] + len(code.encode()) - len(code.lstrip().encode())
            if (name in ('module', 'exports') and fact['origin'] == 'global' and not fact['binding_reassigned']
                    and not fact['escaped'] and fact['writes'] == [{'kind':'object_mutated',
                        'span':[envelope_start, envelope_start + len(name.encode())]}]
                    and _cjs_envelope_only(code, record.get('language'), name)):
                continue
            expected = binding if name == self_name else definitions.get(name)
            initial_self = (validated_rule and expected is not None and expected['path'] == binding['path'] and fact['origin'] == 'module'
                and len(fact['declarations']) == 1 and fact['declarations'][0].get('uninitialized')
                and len(fact['writes']) == 1 and fact['writes'][0] == {
                    'kind':'binding_reassigned', 'span':[expected['byte_span'][0], expected['byte_span'][0] + len(name.encode())]})
            if (fact['binding_reassigned'] and not initial_self) or fact['object_mutated']:
                return fail('consumed_binding_mutated', binding_name=name, binding_path=binding['path'], writes=fact['writes'])
            from . import evidence_shell
            primitive_import = (fact['origin'] == 'module'
                                and evidence_shell._binding(record, side, name, 'sep', module='path'))
            if fact['escaped'] and not primitive_import and (not validated_rule or any(not any(
                    path == binding['path'] and span[0] <= start <= end <= span[1]
                    for path, span in consumed) for start, end in fact['escape_spans'])):
                return fail('consumed_binding_escaped', binding_name=name, binding_path=binding['path'], escape_spans=fact['escape_spans'])
            if name in _INTRINSICS:
                if fact['origin'] != 'global':
                    return fail('intrinsic_binding_not_global')
                continue
            if expected is None:
                # The mechanism proof separately checks how these pinned Node
                # APIs are used. This only authenticates the actual import and
                # proves that the consumed local binding was not reassigned.
                if fact['origin'] == 'module' and (any(evidence_shell._binding(record, side, name, method, module=module)
                        for module, exports in _NODE_EXPORTS.items() for method in exports)
                        or any(evidence_shell._binding(record, side, name, method, promisified=True) for method in ('exec','execFile'))):
                    continue
                return fail('unresolved_consumed_helper', binding_name=name, binding_path=binding['path'])
            if expected['path'] != binding['path']:
                if fact['origin'] != 'module' or not evidence_recursive._link(list(modules.values()), binding['path'], expected['path'], name, record.get('language')):
                    return fail('actual_import_edge_mismatch')
            elif not initial_self and (fact['origin'] not in ('module', 'local') or not fact['declarations'] or any(
                    not expected['byte_span'][0] <= item['span'][0] <= item['span'][1] <= expected['byte_span'][1]
                    for item in fact['declarations'])):
                return fail('actual_declaration_binding_mismatch')
    if not all(_declarative_initialization(item['text'], record.get('language')) for item in modules.values()):
        return fail('module_initialization_unsupported')
    return {'schema': SCHEMA, 'valid': True, 'security_verdict': None,
            'module_bindings': [witness['binding'] for _, witness in witnesses]}
