"""Sanitiser-removal twins for the grounded corpus.

Real mined fixes are rare in the shape the flow analysis can prove. Real code that already sanitises or checks a flowing value is common. This module takes
such a function (the original, which the flow analysis finds free of the weakness) and removes one protection: a wrapping sanitiser call, a validator
statement, or a guard that returns or throws. If the flow analysis then finds a flow of the weakness class that the original did not have, the edited
function is a vulnerable twin whose label is proven by the same analysis, and the original is its fixed form. Nothing is written by a model; the twin is
synthetic and every consumer must say so.
"""
from __future__ import annotations

import pathlib
import re

from app.backend.pipeline import grounded_flow as gf

# Wrapping sanitisers that are effective for the class (the last segment of the callee, case-insensitive). Classes without an entry only use validators and guards.
WRAPPERS = {
    'CWE-79': re.compile(r'^(escape\w*|htmlescape|encodehtml|sanitize\w*|purify|xss|filterxss|cleanhtml)$', re.I),
    'CWE-78': re.compile(r'^(shellescape|escapeshell\w*|escapearg|shquote|quote|number|parseint|parsefloat)$', re.I),
    'CWE-89': re.compile(r'^(escape|escapeid|escapevalue|sqlescape|quoteidentifier|escapeliteral|number|parseint|parsefloat)$', re.I),
    'CWE-22': re.compile(r'^(basename|sanitizefilename|sanitizepath)$', re.I),
    'CWE-1333': re.compile(r'^(escape\w*|regexescape|quotemeta)$', re.I),
}
# Number, parseInt and parseFloat are wrappers for CWE-78 and CWE-89: a numeric coercion of a caller value is a real protection for a command or a query
# (a number cannot carry shell or SQL syntax), so removing it is a twin.
CLASSES = ('CWE-79', 'CWE-78', 'CWE-89', 'CWE-22', 'CWE-918', 'CWE-601', 'CWE-94', 'CWE-1321', 'CWE-1333')
# An unsafe API substituted for the safe one the function used (the edit a careless change makes): execFile with an argument array becomes a shell string,
# a text assignment becomes an HTML assignment.
EXEC_FILE = {'execFile': 'exec', 'execFileSync': 'execSync', 'execFileAsync': 'execAsync', 'execFilePromise': 'execPromise'}
TEXT_TO_HTML = {'textContent': 'innerHTML', 'innerText': 'innerHTML'}
_NOT_MARKUP_ELEMENT = re.compile(r'script|style|template|textarea|title|option', re.I)            # setting innerHTML on these does not parse markup the way a div does
_PROTECTION_NAME = re.compile(r'sanitiz|escap|safe|clean|encod|quot|valid|purif|secure', re.I)
_NEEDS_PARENS = {'binary_expression', 'ternary_expression', 'assignment_expression', 'sequence_expression', 'await_expression', 'arrow_function',
                 'as_expression', 'augmented_assignment_expression'}


def _whole_lines(code_b, start, end):
    """Widen a statement to the lines it occupies when nothing else is on them, so deleting it leaves no blank line behind."""
    line_start = code_b.rfind(b'\n', 0, start) + 1
    if code_b[line_start:start].strip() == b'':
        start = line_start
        tail = end
        while tail < len(code_b) and code_b[tail:tail + 1] in (b' ', b'\t'):
            tail += 1
        if code_b[tail:tail + 1] == b'\n':
            end = tail + 1
            if code_b[:start].endswith(b'\n\n') and code_b[end:end + 1] == b'\n':
                end += 1                            # a statement flanked by blank lines takes one of them with it
    return start, end


def _sites(root, cwe, run):
    """(start_byte, end_byte, replacement_bytes, kind, removed_text) for every protection of the class in source order."""
    pattern = WRAPPERS.get(cwe)
    stack, nodes = [root], []
    while stack:
        node = stack.pop()
        nodes.append(node)
        stack.extend(reversed(node.named_children))
    for node in nodes:
        if node.type == 'call_expression' and pattern is not None:
            args = node.child_by_field_name('arguments')
            callee = node.child_by_field_name('function')
            if args is not None and callee is not None and len(args.named_children) in (1, 2) and pattern.match(gf._last(gf._text(callee))) and (
                    len(args.named_children) == 1 or args.named_children[1].type == 'number'):
                parent = node.parent
                target = parent.child_by_field_name('name') if parent is not None and parent.type == 'variable_declarator' else (
                    parent.child_by_field_name('left') if parent is not None and parent.type == 'assignment_expression' else None)
                if target is not None and _PROTECTION_NAME.search(gf._text(target)):
                    continue                        # "sanitizedName = name" would contradict itself and teach the wrong cue
                arg = args.named_children[0]
                inner = arg.text
                if arg.type in _NEEDS_PARENS:
                    inner = b'(' + inner + b')'
                yield node.start_byte, node.end_byte, inner, 'wrapper', gf._text(node)
        elif node.type == 'ternary_expression':
            condition = node.child_by_field_name('condition')
            branches = [node.child_by_field_name('consequence'), node.child_by_field_name('alternative')]
            value = [b for b in branches if b is not None and b.type not in ('string', 'number', 'null', 'undefined', 'true', 'false', 'template_string')]
            if condition is not None and len(value) == 1 and run.strong_guard(condition):
                inner = value[0].text
                if value[0].type in _NEEDS_PARENS:
                    inner = b'(' + inner + b')'
                yield node.start_byte, node.end_byte, inner, 'guard', gf._text(node)
        elif node.type == 'expression_statement' and node.named_children and node.named_children[0].type == 'call_expression':
            call = node.named_children[0]
            callee = gf._last(gf._text(call.child_by_field_name('function')))
            args = call.child_by_field_name('arguments')
            if gf._is_sanitizer(callee, cwe) and args is not None and args.named_children:
                yield node.start_byte, node.end_byte, None, 'statement', gf._text(node)
        elif node.type == 'if_statement' and node.child_by_field_name('alternative') is None:
            consequence = node.child_by_field_name('consequence')
            exits = any(d.type in ('return_statement', 'throw_statement') for d in gf._descendants(consequence))
            if exits and run.strong_guard(node.child_by_field_name('condition')):
                yield node.start_byte, node.end_byte, None, 'guard', gf._text(node)


def _template_piece(node):
    if node.type == 'string':
        return node.text[1:-1].decode('utf-8', 'replace').replace('`', '\\`').replace('${', '\\${')
    if node.type == 'template_string':
        return node.text[1:-1].decode('utf-8', 'replace')
    return '${' + node.text.decode('utf-8', 'replace') + '}'


def _substitutions(root, cwe):
    """(start_byte, end_byte, replacement_bytes, 'substitution', original_text) for every unsafe substitution of the class, in source order."""
    stack, nodes = [root], []
    while stack:
        node = stack.pop()
        nodes.append(node)
        stack.extend(reversed(node.named_children))
    for node in nodes:
        if cwe == 'CWE-78' and node.type == 'call_expression':
            callee, args = node.child_by_field_name('function'), node.child_by_field_name('arguments')
            name = gf._last(gf._text(callee)) if callee is not None else ''
            if name in EXEC_FILE and args is not None and len(args.named_children) >= 2 and args.named_children[1].type == 'array':
                listed = list(args.named_children[1].named_children)
                if listed and not any(item.type == 'spread_element' for item in listed) and args.named_children[0].type != 'spread_element':
                    command = ' '.join(_template_piece(part) for part in [args.named_children[0], *listed])
                    rest = [gf._text(part) for part in args.named_children[2:]]
                    head = gf._text(callee)
                    head = head[:len(head) - len(name)] + EXEC_FILE[name]
                    yield node.start_byte, node.end_byte, (head + '(`' + command + '`' + ''.join(', ' + part for part in rest) + ')').encode('utf-8'), 'substitution', gf._text(node)
        elif cwe == 'CWE-79':
            if node.type == 'assignment_expression':
                left = node.child_by_field_name('left')
                prop = left.child_by_field_name('property') if left is not None and left.type == 'member_expression' else None
                if prop is not None and gf._text(prop) in TEXT_TO_HTML and not _NOT_MARKUP_ELEMENT.search(gf._text(left.child_by_field_name('object'))):
                    yield prop.start_byte, prop.end_byte, TEXT_TO_HTML[gf._text(prop)].encode(), 'substitution', gf._text(prop)
            elif node.type == 'call_expression':
                callee = node.child_by_field_name('function')
                args = node.child_by_field_name('arguments')
                if (callee is not None and callee.type == 'member_expression' and gf._text(callee.child_by_field_name('property')) == 'text'
                        and args is not None and len(args.named_children) == 1):
                    prop = callee.child_by_field_name('property')
                    yield prop.start_byte, prop.end_byte, b'html', 'substitution', gf._text(prop)


MUTATOR_VERSION = 'mutate-3'          # change with any rule that changes what make_twins returns: it is part of every cache key


def tool_versions():
    """The parser versions a cached result depends on (a different grammar can parse, and so flow, differently)."""
    from importlib import metadata
    out = []
    for name in ('tree-sitter', 'tree-sitter-javascript', 'tree-sitter-typescript'):
        try:
            out.append(f'{name}={metadata.version(name)}')
        except metadata.PackageNotFoundError:
            out.append(f'{name}=absent')
    return ','.join(out)


class TwinCache:
    """A persistent cache of make_twins results, one JSON line per text: key = sha256(mutator version, tool versions, language, text).

    A rebuild of the corpus asks about the same ~24 thousand functions every time; only functions the store gained or changed are computed again."""

    def __init__(self, path):
        import json
        self.path, self.entries, self.new, self.hits, self.misses = pathlib.Path(path), {}, [], 0, 0
        self.prefix = f'{MUTATOR_VERSION}|{tool_versions()}|'
        if self.path.is_file():
            for line in self.path.read_text(encoding='utf-8').splitlines():
                if line.strip():
                    row = json.loads(line)
                    self.entries[row['key']] = row['twins']

    def key(self, code, language):
        import hashlib
        return hashlib.sha256((self.prefix + language + '|' + (code or '')).encode('utf-8')).hexdigest()

    def twins(self, code, language):
        key = self.key(code, language)
        if key in self.entries:
            self.hits += 1
            return self.entries[key]
        self.misses += 1
        found = make_twins(code, language)
        self.entries[key] = found
        self.new.append({'key': key, 'twins': found})
        return found

    def save(self):
        import json
        if not self.new:
            return 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open('a', encoding='utf-8', newline='\n') as stream:
            for row in self.new:
                stream.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + '\n')
        saved, self.new = len(self.new), []
        return saved


def make_twins(code, language):
    if language == 'python':
        from app.backend.pipeline import grounded_mutate_py
        return grounded_mutate_py.make_twins(code)
    return _make_js_twins(code, language)


def _make_js_twins(code, language):
    """At most one twin of the function: {'cwe', 'kind', 'removed', 'original', 'mutated', 'flow'}; [] when none can be proven."""
    code = re.sub(r'\n{3,}', '\n\n', code or '')                 # runs of blank lines carry nothing and would show where a statement was deleted
    parsed = gf.parse_variant(code, language) if code and language in gf.LANGUAGES else None
    if parsed is None:
        return []
    root, lines, _text, prefix = parsed
    code_b = code.encode('utf-8')
    for cwe in CLASSES:
        if not gf.might_have_sink(code, cwe):
            continue                                    # no sink word of the class: no flow can appear after removing a protection either
        if gf.find_flows(code, language, cwe):
            continue                                    # the original is not clean for this class, so a twin would prove nothing
        run = gf._Run(cwe, lines)
        for start, end, replacement, kind, removed in [*_sites(root, cwe, run), *_substitutions(root, cwe)]:
            s, e = start - prefix, end - prefix
            if s < 0 or e > len(code_b):
                continue
            if replacement is None:
                s, e = _whole_lines(code_b, s, e)
                replacement = b''
            mutated = (code_b[:s] + replacement + code_b[e:]).decode('utf-8', 'replace')
            if mutated == code:
                continue
            flows = gf.find_flows(mutated, language, cwe)
            if flows:
                return [{'cwe': cwe, 'kind': kind, 'removed': removed, 'original': code, 'mutated': mutated, 'flow': flows[0]}]
    return []
