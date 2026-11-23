"""Sanitiser-removal and unsafe-substitution twins for Python units (see grounded_mutate for the idea and the proof rule).

A real function that the flow analysis finds free of a weakness class is edited once: a protection is removed (a wrapping sanitiser or coercion, a validator statement
or assert, a guard that raises or returns) or a safe call is replaced by its unsafe form (argv list to a shell string, yaml.safe_load to yaml.load, ast.literal_eval to
eval). The edit is kept only if the analysis then finds a flow of that class. The twin is synthetic and every consumer must say so. Twins use the dedented code of the unit.
"""
from __future__ import annotations

import re
import textwrap

from app.backend.pipeline import grounded_flow_py as py

CLASSES = ('CWE-78', 'CWE-94', 'CWE-22', 'CWE-918', 'CWE-89', 'CWE-79', 'CWE-601', 'CWE-502', 'CWE-1333')
WRAPPERS = {
    'CWE-78': re.compile(r'^(?:shlex\.)?quote$|^int$|^float$', re.I),
    'CWE-89': re.compile(r'^int$|^float$|^escape_string$|^quote_ident$|^quote$', re.I),
    'CWE-22': re.compile(r'^(?:os\.path\.)?basename$|^secure_filename$', re.I),
    'CWE-79': re.compile(r'^(?:html\.)?escape$|^(?:markupsafe\.)?escape$|^bleach\.clean$|^sanitize\w*$|^strip_tags$', re.I),
    'CWE-601': re.compile(r'^quote$|^urllib\.parse\.quote$', re.I),
    'CWE-1333': re.compile(r'^re\.escape$|^escape$', re.I),
}
_PROTECTION_NAME = re.compile(r'sanitiz|escap|safe|clean|encod|quot|valid|purif|secure', re.I)
_NEEDS_PARENS = {'binary_operator', 'conditional_expression', 'boolean_operator', 'await', 'lambda', 'not_operator', 'comparison_operator'}


def _whole_lines(code_b, start, end):
    line_start = code_b.rfind(b'\n', 0, start) + 1
    if code_b[line_start:start].strip() == b'':
        start = line_start
        if code_b[end:end + 1] == b'\n':
            end += 1
    return start, end


def _nodes(root):
    stack, out = [root], []
    while stack:
        node = stack.pop()
        out.append(node)
        stack.extend(reversed(node.named_children))
    return out


def _sites(root, cwe, run):
    """(start_byte, end_byte, replacement_bytes | None, kind, removed_text) for every edit of the class, in source order."""
    pattern = WRAPPERS.get(cwe)
    for node in _nodes(root):
        if node.type == 'call' and pattern is not None:
            callee, args = node.child_by_field_name('function'), node.child_by_field_name('arguments')
            name = py._text(callee) if callee is not None else ''
            if args is not None and len(args.named_children) == 1 and args.named_children[0].type != 'keyword_argument' and (pattern.match(name) or pattern.match(py._last(name))):
                parent = node.parent
                target = parent.child_by_field_name('left') if parent is not None and parent.type == 'assignment' else None
                if target is not None and _PROTECTION_NAME.search(py._text(target)):
                    continue
                inner = args.named_children[0]
                text = inner.text
                if inner.type in _NEEDS_PARENS:
                    text = b'(' + text + b')'
                yield node.start_byte, node.end_byte, text, 'wrapper', py._text(node)
        if node.type == 'call' and node.parent is not None and node.parent.type in ('block', 'expression_statement'):
            callee, args = node.child_by_field_name('function'), node.child_by_field_name('arguments')
            if callee is not None and py._is_sanitizer(py._last(py._text(callee))) and args is not None and args.named_children:
                target = node.parent if node.parent.type == 'expression_statement' else node
                yield target.start_byte, target.end_byte, None, 'statement', py._text(target)
        elif node.type == 'assert_statement':
            if any(c.type == 'call' and py._is_sanitizer(py._last(py._text(c.child_by_field_name('function')))) for c in _nodes(node)):
                yield node.start_byte, node.end_byte, None, 'statement', py._text(node)
        elif node.type == 'if_statement' and not [c for c in node.named_children if c.type in ('else_clause', 'elif_clause')]:
            consequence = node.child_by_field_name('consequence')
            exits = consequence is not None and any(d.type in ('return_statement', 'raise_statement') for d in _nodes(consequence))
            if exits and run.checked(node.child_by_field_name('condition')):
                yield node.start_byte, node.end_byte, None, 'guard', py._text(node)
        if cwe == 'CWE-78' and node.type == 'call':
            callee, args = node.child_by_field_name('function'), node.child_by_field_name('arguments')
            full = py._text(callee) if callee is not None else ''
            if full.startswith('subprocess.') and py._last(full) in py._SUBPROCESS and args is not None and args.named_children:
                first = args.named_children[0]
                if first.type == 'list' and first.named_children and not any(c.type in ('list_splat',) for c in first.named_children):
                    parts = []
                    for item in first.named_children:
                        parts.append(item.text.decode('utf-8', 'replace')[1:-1] if item.type == 'string' and item.text[:1] in (b'"', b"'") else '{' + item.text.decode('utf-8', 'replace') + '}')
                    rest = [py._text(a) for a in args.named_children[1:]]
                    if not any(r.startswith('shell') for r in rest):
                        shell = 'f"' + ' '.join(parts).replace('"', "'") + '"'
                        yield first.start_byte, first.end_byte, (shell + ', shell=True').encode('utf-8'), 'substitution', py._text(first)
        if cwe == 'CWE-502' and node.type == 'call':
            callee = node.child_by_field_name('function')
            if callee is not None and py._text(callee) == 'yaml.safe_load':
                yield callee.start_byte, callee.end_byte, b'yaml.load', 'substitution', py._text(callee)
        if cwe == 'CWE-94' and node.type == 'call':
            callee = node.child_by_field_name('function')
            if callee is not None and py._text(callee) in ('ast.literal_eval', 'literal_eval'):
                yield callee.start_byte, callee.end_byte, b'eval', 'substitution', py._text(callee)


def make_twins(code):
    """At most one twin: {'cwe', 'kind', 'removed', 'original', 'mutated', 'flow'}; [] when none can be proven."""
    code = re.sub(r'\n{3,}', '\n\n', textwrap.dedent(code or ''))
    parsed = py.parse(code) if code.strip() and py.might_have_sink(code) or any(w in code for w in ('safe_load', 'literal_eval', 'quote', 'escape', 'basename', 'secure_filename', 'int(', 'float(')) else None
    if parsed is None:
        return []
    root, lines, text, _ = parsed
    code_b = text.encode('utf-8')
    for cwe in CLASSES:
        if py.find_flows(text, cwe):
            continue                                        # the original is not clean for this class, so a twin would prove nothing
        run = py._Run(cwe, lines)
        for start, end, replacement, kind, removed in _sites(root, cwe, run):
            if replacement is None:
                start, end = _whole_lines(code_b, start, end)
                replacement = b''
            mutated = (code_b[:start] + replacement + code_b[end:]).decode('utf-8', 'replace')
            if mutated == text or py.parse(mutated) is None:
                continue
            flows = py.find_flows(mutated, cwe)
            if flows:
                return [{'cwe': cwe, 'kind': kind, 'removed': removed, 'original': text, 'mutated': mutated, 'flow': flows[0]}]
    return []
