"""Bridge from a grounded unit to the trusted-execution planner (app/backend/pipeline/verify/automatic.py).

The planner runs an exact mined function in an AST-restricted Node context inside a pinned, no-network container and compares an attack and a benign
probe set on the vulnerable and the fixed side. It was written for reviewed mined records (with commits and context items), so a grounded unit needs an
adapter: the import header of its file as a context item, and, for a synthetic twin, commit ids that can never pass for real ones. Nothing here starts a
container; `coverage_row` only plans.
"""
from __future__ import annotations

import hashlib
import json
import re

from app.backend.pipeline import grounded_flow as gf
from app.backend.pipeline import grounded_views as views_lib

MAX_HEADER_CHARS = 4000
DUMMY_IMAGE = 'sha256:' + '0' * 64          # planning never touches the image; a placeholder lets plan_case run without Docker


def _is_require(node):
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == 'call_expression':
            callee = current.child_by_field_name('function')
            if callee is not None and gf._text(callee) == 'require':
                return True
        stack.extend(current.named_children)
    return False


_PY_IMPORT = re.compile(r'^(?:import\s+[\w.]+(?:\s+as\s+\w+)?(?:\s*,\s*[\w.]+(?:\s+as\s+\w+)?)*|from\s+[\w.]+\s+import\s+(?:\([^)]*\)|[^\n]+))\s*$', re.M | re.S)


def python_import_header(text):
    """The leading imports of a Python file as written (module docstring, comments, __future__ and blank lines skipped), stopping at the first other statement."""
    parts, size = [], 0
    lines = (text or '').split('\n')
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            index += 1
            continue
        if stripped.startswith(('"""', "\'\'\'")):                                   # a module docstring
            quote = stripped[:3]
            if stripped.count(quote) >= 2 and len(stripped) > 3:
                index += 1
                continue
            index += 1
            while index < len(lines) and quote not in lines[index]:
                index += 1
            index += 1
            continue
        if stripped.startswith(('import ', 'from ')) and not line.startswith((' ', '\t')):
            block = [line]
            if '(' in line and ')' not in line:                                          # a parenthesised import runs over several lines
                while index + 1 < len(lines) and ')' not in block[-1]:
                    index += 1
                    block.append(lines[index])
            statement = '\n'.join(block)
            if size + len(statement) + 1 > MAX_HEADER_CHARS:
                break
            parts.append(statement)
            size += len(statement) + 1
            index += 1
            continue
        break
    return '\n'.join(parts)


def import_header(text, language):
    """The leading imports and requires of a source file as written (directives and comments skipped), stopping at the first other statement.

    The planner reads these to learn which names are the Node builtins path and child_process; anything it cannot parse gives an empty header."""
    if language == 'python':
        return python_import_header(text)
    if not text or language not in gf.LANGUAGES:
        return ''
    parser = gf._parser(gf.LANGUAGES[language])
    if parser is None:
        return ''
    tree = parser.parse(text.encode('utf-8'))
    if tree.root_node.has_error:
        return ''
    parts, size = [], 0
    for node in tree.root_node.named_children:
        if node.type == 'comment' or (node.type == 'expression_statement' and node.named_children and node.named_children[0].type == 'string'):
            continue
        if node.type == 'import_statement' or (node.type in ('lexical_declaration', 'variable_declaration') and _is_require(node)):
            statement = gf._text(node)
            if size + len(statement) + 1 > MAX_HEADER_CHARS:
                break
            parts.append(statement)
            size += len(statement) + 1
            continue
        break
    return '\n'.join(parts)


_IDENT = re.compile(r'[A-Za-z_$][\w$]*')


def _params_text(node, params):
    if params is None:
        return None
    text = gf._text(params)
    return text if text.startswith('(') else f'({text})'


def planner_shape(code, language):
    """The code as one plain top-level function, the only form the planner reads: a method, an object-property function, an arrow property or an exported
    function is rewritten to `function name(params) { body }`. Anything else is returned unchanged, so the planner makes its own refusal."""
    parsed = gf.parse_variant(code, language) if code and language in gf.LANGUAGES else None
    if parsed is None:
        return code
    root, _lines, _text, prefix = parsed
    members = list(root.named_children)
    if prefix == 0 and len(members) == 1 and members[0].type == 'labeled_statement':      # "name: (a) => {...}" reads as a label when parsed bare
        parser = gf._parser(gf.LANGUAGES[language])
        wrapped = parser.parse(('({\n' + code.rstrip().rstrip(',') + '\n})\n').encode('utf-8'))
        if not wrapped.root_node.has_error:
            root, prefix, members = wrapped.root_node, 3, list(wrapped.root_node.named_children)
    if prefix == 0:
        if len(members) == 1 and members[0].type == 'export_statement':
            declaration = members[0].child_by_field_name('declaration')
            if declaration is not None and declaration.type == 'function_declaration':
                return gf._text(declaration) + '\n'
        return code
    if len(members) != 1:
        return code
    wrapper = members[0]
    if wrapper.type == 'class_declaration':                                  # a bare method, parsed inside a class
        body = wrapper.child_by_field_name('body')
        items = [n for n in (body.named_children if body is not None else []) if n.type != 'comment']
        if len(items) != 1 or items[0].type != 'method_definition':
            return code
        method = items[0]
        name = method.child_by_field_name('name')
        params, block = method.child_by_field_name('parameters'), method.child_by_field_name('body')
        if name is None or name.type not in ('property_identifier', 'identifier') or not _IDENT.fullmatch(gf._text(name)) or params is None or block is None:
            return code
        asynchronous = any(child.type == 'async' for child in method.children)
        return f"{'async ' if asynchronous else ''}function {gf._text(name)}{gf._text(params)} {gf._text(block)}\n"
    if wrapper.type == 'expression_statement' and wrapper.named_children and wrapper.named_children[0].type == 'parenthesized_expression':
        obj = wrapper.named_children[0].named_children[0] if wrapper.named_children[0].named_children else None
        pairs = [n for n in (obj.named_children if obj is not None and obj.type == 'object' else []) if n.type != 'comment']
        if len(pairs) != 1 or pairs[0].type != 'pair':
            return code
        key, value = pairs[0].child_by_field_name('key'), pairs[0].child_by_field_name('value')
        name = gf._text(key).strip('\'"') if key is not None else ''
        if value is None or not _IDENT.fullmatch(name) or value.type not in ('function_expression', 'function', 'arrow_function'):
            return code
        params = value.child_by_field_name('parameters') or value.child_by_field_name('parameter')
        block = value.child_by_field_name('body')
        text = _params_text(value, params)
        if text is None or block is None or block.type != 'statement_block':
            return code
        asynchronous = any(child.type == 'async' for child in value.children)
        return f"{'async ' if asynchronous else ''}function {name}{text} {gf._text(block)}\n"
    return code


def _sha(text):
    return hashlib.sha256((text or '').encode('utf-8')).hexdigest()[:40]


def unit_to_record(unit):
    """The record dict that automatic.plan_case reads. Pure; the same unit always gives the same record."""
    origin = unit.get('origin', 'upstream_fix')
    header = unit.get('exec_context') or ''
    file = unit['file']
    context = json.dumps({'items': [{'kind': 'imports', 'path': file, 'text': header}] if header else []}, sort_keys=True)
    if origin == 'upstream_fix' and unit.get('commit_key'):
        parent, fix = f"{unit['commit_key']}^", unit['commit_key']
    else:                                     # a twin has no commits; the prefix keeps it from being taken for a real fix
        parent, fix = 'syn-before:' + _sha(unit['before']), 'syn-after:' + _sha(unit['after'])
    before, after = planner_shape(unit['before'], unit['language']), planner_shape(unit['after'], unit['language'])
    return {'record_id': unit['unit_id'], 'raw_pair_key': str(unit.get('pair_id')), 'vulnerable_code': before, 'fixed_code': after,
            'language': unit['language'], 'cwe_ids': list(unit.get('cwe') or []), 'file_path_before': file, 'file_path_after': file,
            'symbol': views_lib.symbol(before), 'relevant_parent_commit': parent, 'fix_commit': fix,
            'context_before': context, 'context_after': context, 'repository_identity': unit.get('repo_url') or unit.get('package'),
            'ecosystem': 'npm', 'provenance_status': None, 'basis': origin}


def coverage_row(unit):
    """Whether the planner accepts the unit and, if not, the mechanism it recognised and the reason (no container, no image needed)."""
    from app.backend.pipeline.verify import automatic
    record = unit_to_record(unit)
    case = automatic.plan_case(record, image_id=DUMMY_IMAGE)
    if case is not None:
        return {'unit_id': unit['unit_id'], 'origin': record['basis'], 'cwe': list(unit.get('cwe') or []), 'accepted': True,
                'mechanism': case.mechanism, 'reason': 'runtime_case_available', 'missing_facts': []}
    diagnosis = automatic.diagnose_case(record)
    return {'unit_id': unit['unit_id'], 'origin': record['basis'], 'cwe': list(unit.get('cwe') or []), 'accepted': False,
            'mechanism': diagnosis.get('mechanism'), 'reason': diagnosis.get('reason'), 'missing_facts': list(diagnosis.get('missing_facts') or [])}
