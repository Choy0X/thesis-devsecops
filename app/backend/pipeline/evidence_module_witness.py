"""Resolve bounded exact proof source independently of student prompt budgets.

A witness authenticates the selected module/unit relationship. It grants no
security verdict, does not execute source, and never edits context or raw data.
The normal backend snapshot provider performs no implicit network acquisition.
"""
from __future__ import annotations

import hashlib
import json
import re

SCHEMA = 'exact-module-witness/1'
_UNITS = {'function_declaration', 'function_expression', 'arrow_function', 'method_definition',
          'lexical_declaration', 'variable_declaration', 'export_statement', 'expression_statement'}


def _complete_unit(node, kind='function'):
    if node.type == 'export_statement':
        node = node.child_by_field_name('declaration') or node.child_by_field_name('value')
    if node is None:
        return False
    if kind == 'declaration' and node.type in ('lexical_declaration', 'variable_declaration', 'function_declaration'):
        return True
    if node.type in ('lexical_declaration', 'variable_declaration', 'expression_statement'):
        children = [child for child in node.named_children if child.type != 'comment']
        if len(children) != 1:
            return False
        declaration = children[0]
        if declaration.type == 'variable_declarator':
            node = declaration.child_by_field_name('value')
        elif declaration.type == 'assignment_expression':
            node = declaration.child_by_field_name('right')
        else:
            return False
    return node is not None and node.type in ('function_declaration', 'function_expression', 'arrow_function', 'method_definition')


def _object(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value


def resolve(record, side, *, provider=None, target_kind='function'):
    from . import security_snapshots, evidence_ast
    if side not in ('before', 'after'):
        raise ValueError('A module witness needs an explicit before/after side.')
    if target_kind not in ('function', 'declaration', 'module'):
        raise ValueError('Unsupported module witness target kind.')
    def fail(cause):
        return {'schema': SCHEMA, 'state': 'unavailable', 'cause': cause, 'security_verdict': None}
    revision = record.get('relevant_parent_commit' if side == 'before' else 'fix_commit')
    if not re.fullmatch(r'[0-9a-fA-F]{40,64}', str(revision or '')):
        return fail('exact_revision_required')
    try:
        path = security_snapshots.logical_path(record.get('file_path_' + side))
    except (ValueError, TypeError, AttributeError):
        return fail('unsafe_source_path')
    code = record.get('vulnerable_code' if side == 'before' else 'fixed_code')
    if not isinstance(code, str) or not code.strip():
        return fail('target_source_missing')
    if provider is None:
        from .evidence_recovery import _source_row
        provider = lambda row, rev: security_snapshots.snapshot_for(_source_row(row), rev)
    try:
        snapshot = provider(record, revision)
        if snapshot.revision != revision:
            return fail('snapshot_revision_mismatch')
        text = snapshot.read(path) if snapshot.available else None
    except (OSError, ValueError):
        return fail('exact_module_unavailable')
    if not isinstance(text, str):
        return fail('exact_module_unavailable')
    raw, target = text.encode('utf-8'), code.encode('utf-8')
    if len(raw) > security_snapshots.MAX_BLOB_BYTES:
        return fail('module_source_budget_exceeded')
    if target not in raw:
        return fail('target_source_mismatch')
    module_sha = hashlib.sha256(raw).hexdigest()
    target_sha = hashlib.sha256(target).hexdigest()
    context = _object(record.get('context_' + side) or '{}')
    if not isinstance(context, dict):
        return fail('malformed_source_context')
    saved = context.get('snapshot') or {}
    if not isinstance(saved, dict):
        return fail('malformed_source_context')
    if ((saved.get('revision') is not None and saved['revision'] != revision)
            or (saved.get('side') is not None and saved['side'] != side)
            or (saved.get('path') is not None and saved['path'] != path)):
        return fail('context_source_binding_mismatch')
    files = saved.get('files') or {}
    if not isinstance(files, dict):
        return fail('malformed_source_context')
    if any(value is not None and value != module_sha for value in (saved.get('file_sha256'), files.get(path))):
        return fail('module_hash_mismatch')
    if saved.get('target_sha256') is not None and saved['target_sha256'] != target_sha:
        return fail('target_hash_mismatch')
    span = _object(record.get('byte_span_' + side))
    if span is None:
        if raw.count(target) != 1:
            return fail('ambiguous_target_source')
        start = raw.index(target)
        span = [start, start + len(target)]
    if (not isinstance(span, list) or len(span) != 2 or any(type(value) is not int for value in span)
            or not 0 <= span[0] <= span[1] <= len(raw) or raw[span[0]:span[1]] != target):
        return fail('target_span_mismatch')
    parsed = evidence_ast.parse_snapshot(record.get('language'), text)
    if parsed is None:
        return fail('exact_module_parse_failure')
    tree, _ = parsed
    if target_kind == 'module':
        if target != raw:
            return fail('target_is_not_complete_module')
        unit = 'program'
    else:
        unit = None
    start = span[0] + len(target) - len(target.lstrip())
    end = span[1] - len(target) + len(target.rstrip())
    stack, visited = [tree] if unit is None else [], 0
    while stack:
        node = stack.pop()
        visited += 1
        if visited > 100000:
            return fail('module_analysis_budget_exceeded')
        if node.start_byte == start and node.end_byte == end and node.type in _UNITS and _complete_unit(node, target_kind):
            unit = node.type
            break
        if node.start_byte <= start and node.end_byte >= end:
            stack.extend(node.named_children)
    if unit is None:
        return fail('target_is_not_complete_unit')
    return {'schema': SCHEMA, 'state': 'available', 'security_verdict': None, 'module_text': text,
        'binding': {'record_id': record.get('record_id'), 'repository': record.get('repository_identity'),
            'revision': revision, 'side': side, 'path': path, 'module_sha256': module_sha,
            'target_sha256': target_sha, 'byte_span': span, 'unit_type': unit,
            'grammar': evidence_ast.grammar_signature()}}
