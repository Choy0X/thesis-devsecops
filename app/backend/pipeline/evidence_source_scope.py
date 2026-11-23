"""Exact lexical and runtime context recovery, never a security proof."""
from __future__ import annotations

import hashlib
import json
import posixpath


def exported_definition(source, language, name):
    """Recover a named declaration, without asserting module initialization safety.

    The strict mechanism validator still decides whether all surrounding module
    semantics are supported. Recovery must not confuse those semantics with an
    absent file or declaration.
    """
    from . import evidence_ast, evidence_exports, evidence_rules as rules
    parsed = evidence_ast.parse_snapshot(language, source)
    if parsed is None:
        return None
    root, data = parsed
    public = evidence_exports.bindings(source, language).get(name)
    exports = []
    for statement in rules.children(root):
        if statement.type != 'expression_statement':
            continue
        for expression in statement.named_children:
            if expression.type != 'assignment_expression':
                continue
            left = rules.text(expression.child_by_field_name('left'), data)
            if left in ('exports.' + name, 'module.exports.' + name):
                exports.append(expression.child_by_field_name('right'))
            elif left in ('exports', 'module.exports'):
                # Whole export replacement cannot be combined with a named edge.
                return None
    if public == {'local': name}:
        if exports:
            return None
    elif len(exports) == 1 and rules.text(exports[0], data) == name:
        pass
    else:
        return None
    found = []
    for statement in rules.children(root):
        node = statement.child_by_field_name('declaration') if statement.type == 'export_statement' else statement
        if node is None:
            continue
        if node.type == 'function_declaration' and rules.text(node.child_by_field_name('name'), data) == name:
            found.append(rules.text(node, data))
        elif node.type in ('lexical_declaration', 'variable_declaration'):
            if any(child.type == 'variable_declarator' and rules.text(child.child_by_field_name('name'), data) == name
                   for child in node.named_children):
                found.append(rules.text(node, data))
    for node in rules.walk(root):
        if node.type in ('assignment_expression', 'augmented_assignment_expression', 'update_expression'):
            target = node.child_by_field_name('left') or node.child_by_field_name('argument')
            if target is not None and target.type == 'identifier' and rules.text(target, data) == name:
                return None
    return found[0] if len(found) == 1 else None


def lexical_item(source, target, path, language, name):
    from . import evidence_ast
    from .verify.closure_diagnostics import free_values, FUNCTIONS
    parsed = evidence_ast.parse_snapshot(language, source)
    if parsed is None or not target.strip() or source.count(target.strip()) != 1:
        return None
    root, data = parsed
    start = len(source[:source.index(target.strip())].encode())
    end = start + len(target.strip().encode())
    if name not in (free_values(target, language) or ()):
        return None
    nodes, pending = [], [root]
    while pending:
        node = pending.pop()
        if node.start_byte <= start and node.end_byte >= end:
            if node.type in FUNCTIONS and (node.start_byte < start or node.end_byte > end):
                nodes.append(node)
            pending.extend(node.named_children)
    for node in sorted(nodes, key=lambda n: n.end_byte-n.start_byte):
        text = data[node.start_byte:node.end_byte].decode()
        missing = free_values(text, language)
        if missing is not None and name not in missing:
            return {'kind': 'lexical_scope', 'path': path, 'name': name, 'text': text,
                    'target_sha256': hashlib.sha256(target.encode()).hexdigest(),
                    'reason': 'Exact enclosing lexical scope; values and security semantics still require validation.'}
    return None


def environment_item(snapshot, path, name):
    """Use nearest exact package manifest; absence does not imply a Node runtime."""
    from .verify.closure_diagnostics import NODE, COMMONJS
    parent = posixpath.dirname(path)
    while True:
        manifest = posixpath.join(parent, 'package.json') if parent else 'package.json'
        text = snapshot.read(manifest)
        if text is not None:
            try:
                value = json.loads(text)
            except (ValueError, TypeError):
                return [], {}, 'invalid_runtime_manifest'
            if not isinstance(value, dict):
                return [], {}, 'invalid_runtime_manifest'
            suffix = posixpath.splitext(path)[1]
            engines = value.get('engines')
            node = (isinstance(engines, dict) and bool(engines.get('node'))
                    or value.get('type') in ('commonjs', 'module')
                    or bool(value.get('main') or value.get('bin') or value.get('exports')))
            commonjs = suffix == '.cjs' or suffix in ('.js', '.ts', '.cts') and value.get('type') != 'module'
            supplied = set(NODE) if node else set()
            if node and commonjs:
                supplied.update(COMMONJS)
            if name not in supplied:
                return [], {}, 'node_environment_does_not_supply'
            return [{'kind': 'runtime_environment', 'path': manifest, 'name': None, 'text': text,
                     'reason': 'Exact nearest package manifest establishes the declared Node execution environment.'}], {manifest: text}, None
        if not parent:
            return [], {}, 'exact_runtime_manifest_unavailable'
        parent = posixpath.dirname(parent)
