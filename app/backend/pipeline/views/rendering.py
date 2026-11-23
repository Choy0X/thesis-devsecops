"""Versioned student rendering, downstream of immutable source admission.

This module does not admit records, assign labels, edit snapshots or issue proof
receipts. Its inputs must already be selected within the original context budget.
Equivalence is limited to the pinned parser's runtime tokens and AST. The default
rejects known directive-sensitive comments. An explicit SFT display variant
preserves only three exact canonical Istanbul coverage comments. This module
does not certify transpiler, compiler, coverage, debugger or custom-plugin
behavior. Renderings are display inputs, never replacement compilation inputs.
"""
from __future__ import annotations

import copy
import hashlib
import json
import pathlib
import re

from app.backend.pipeline import evidence_ast, evidence_shell

POLICY = 'ast-comment-blinding/1'
ISTANBUL_POLICY = 'ast-comment-blinding-preserve-istanbul/1'
CANONICAL_ISTANBUL = frozenset(('/* istanbul ignore else */', '/* istanbul ignore if */',
                               '/* istanbul ignore next */'))
DETECTION_TASKS = frozenset(('discovery', 'condition_assessment', 'localization'))
CODE_KINDS = frozenset(('enclosing', 'callee', 'caller', 'imports', 'module_symbol'))
UNCHANGED_KINDS = frozenset(('manifest', 'configuration'))
IDENTIFIER = re.compile(r'\b(?:CVE-\d{4}-\d{4,}|GHSA-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}|CWE-\d+)\b', re.I)
LINE_TERMINATORS = frozenset('\r\n\u2028\u2029')
MAX_NODES, MAX_DEPTH = 12000, 96
WRAPPER_PREFIX = 'class Subject {\n'
DIRECTIVE_HINT = re.compile(
    r'\b(?:source(?:mapping)?url|ts-(?:ignore|expect-error|nocheck|check)|'
    r'istanbul|c8|v8|nyc|coverage|bun|webpack\w*|eslint\w*|prettier\w*|'
    r'jshint|jslint|flow|jsx(?:ImportSource|Runtime)?|cc_on|pure|'
    r'no_side_effects|babel\w*|rollup\w*|esbuild\w*|swc)\b', re.I)


class RenderingError(ValueError):
    pass


def implementation_hash():
    return hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest()


def _policy(*, preserve_istanbul_comments=False):
    if not isinstance(preserve_istanbul_comments, bool):
        raise RenderingError('rendering_mode_unsupported')
    return ISTANBUL_POLICY if preserve_istanbul_comments else POLICY


def identity(*, preserve_istanbul_comments=False):
    return {'policy': _policy(preserve_istanbul_comments=preserve_istanbul_comments),
            'implementation_sha256': implementation_hash(),
            'grammar': evidence_ast.grammar_signature(), 'scope': 'parser_tokens_and_ast_only',
            'toolchain_behavior_verified': False}


def input_fingerprint(task, target, items, extra, *, preserve_istanbul_comments=False):
    payload = {'task': task, 'target': target, 'items': items, 'extra': extra,
               'policy': _policy(preserve_istanbul_comments=preserve_istanbul_comments),
               'implementation_sha256': implementation_hash()}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def _sha(source):
    return hashlib.sha256(source.encode('utf-8')).hexdigest()


def _boundaries(source):
    result = [0]
    for character in source:
        result.append(result[-1] + len(character.encode('utf-8')))
    return result


def _parse(source, language):
    if not isinstance(source, str) or not source:
        raise RenderingError('source_missing')
    parsed = evidence_ast.parse(language, source)
    if parsed is not None:
        return parsed, 0, 'none'
    parsed = evidence_shell._parse_unit(language, source)
    if parsed is None:
        raise RenderingError('source_parse_unsupported')
    tree, raw = parsed
    classes = [n for n in tree.named_children if n.type != 'comment']
    if len(classes) != 1 or classes[0].type != 'class_declaration':
        raise RenderingError('wrapper_not_single_method')
    body = classes[0].child_by_field_name('body')
    members = [n for n in body.named_children if n.type != 'comment'] if body is not None else []
    if len(members) != 1 or members[0].type != 'method_definition' or not evidence_shell._unit_owner(members[0]):
        raise RenderingError('wrapper_not_single_method')
    if raw.decode() != WRAPPER_PREFIX + source + '\n}':
        raise RenderingError('wrapper_source_mismatch')
    return parsed, len(WRAPPER_PREFIX), 'single_method_class'


def _inventory(tree, raw, offset, source_bytes):
    """Exact non-comment AST plus source-owned token/comment byte intervals."""
    comments, tokens, count = [], [], [0]
    def visit(node, depth):
        count[0] += 1
        if count[0] > MAX_NODES or depth > MAX_DEPTH:
            raise RenderingError('rendering_budget_exceeded')
        start, end = node.start_byte - offset, node.end_byte - offset
        # These are separate named kinds in all three pinned grammars. Annex B
        # comments are not executable evidence; a hashbang can affect the real
        # interpreter invocation. Neither kind is supported by this renderer.
        if node.type in ('html_comment', 'hash_bang_line'):
            raise RenderingError('comment_ast_kind_unsupported')
        if node.type == 'comment':
            if not 0 <= start <= end <= source_bytes:
                raise RenderingError('comment_outside_source')
            comments.append((start, end))
            return None
        if node.children:
            return (node.type, tuple(value for child in node.children
                                     if (value := visit(child, depth + 1)) is not None))
        token = raw[node.start_byte:node.end_byte]
        if token and 0 <= start < end <= source_bytes:
            tokens.append((start, end))
        return node.type, token
    signature = visit(tree, 0)
    return signature, sorted(comments), sorted(tokens)


def render_code(source, language, *, preserve_istanbul_comments=False):
    """Return one rendering and a reversible coordinate map, never a verdict."""
    policy = _policy(preserve_istanbul_comments=preserve_istanbul_comments)
    parsed, offset, wrapper = _parse(source, language)
    tree, raw = parsed
    signature, comments, token_bytes = _inventory(tree, raw, offset, len(source.encode()))
    original_boundaries = _boundaries(source)
    character_of_byte = {boundary: index for index, boundary in enumerate(original_boundaries)}
    shown, comment_chars, preserved = list(source), [], 0
    for start, end in comments:
        if start not in character_of_byte or end not in character_of_byte:
            raise RenderingError('comment_not_character_aligned')
        a, b = character_of_byte[start], character_of_byte[end]
        value = source[a:b]
        # This explicit SFT display variant keeps the entire exact directive;
        # it does not erase a pragma or claim that coverage tooling was tested.
        # Any prose, identifier, annotation or other extension fails the exact
        # allowlist and reaches the unchanged directive rejection below.
        if preserve_istanbul_comments and value in CANONICAL_ISTANBUL:
            comment_chars.append((a, b))
            preserved += 1
            continue
        # Parser AST equality cannot establish that comments are inert to a
        # compiler or tool. Deliberately reject all JSDoc/triple-slash forms,
        # annotation markers and common tool directives, even if this also
        # rejects ordinary prose. Arbitrary custom toolchains remain outside
        # the reported equivalence scope rather than silently being certified.
        if value.startswith(('///', '/**')) or '@' in value or '#' in value or DIRECTIVE_HINT.search(value):
            raise RenderingError('comment_directive_unsupported')
        if value.startswith('//'):
            left, right = a + 2, b
        elif value.startswith('/*') and value.endswith('*/'):
            left, right = a + 2, b - 2
        else:
            raise RenderingError('comment_form_unsupported')
        for index in range(left, right):
            if shown[index] not in LINE_TERMINATORS:
                shown[index] = ' '
        comment_chars.append((a, b))
    rendered = ''.join(shown)
    reparsed, new_offset, new_wrapper = _parse(rendered, language)
    new_signature, new_comments, _ = _inventory(*reparsed, new_offset, len(rendered.encode()))
    if new_signature != signature or wrapper != new_wrapper:
        raise RenderingError('executable_ast_changed')
    rendered_boundaries = _boundaries(rendered)
    new_character_of_byte = {boundary: index for index, boundary in enumerate(rendered_boundaries)}
    if [(new_character_of_byte[a], new_character_of_byte[b]) for a, b in new_comments] != comment_chars:
        raise RenderingError('comment_boundaries_changed')
    token_chars = [(character_of_byte[a], character_of_byte[b]) for a, b in token_bytes]
    for a, b in token_chars:
        if source[a:b].encode() != rendered[a:b].encode():
            raise RenderingError('executable_token_changed')
    result = {'rendered': rendered, 'source_map': {
        'character_mapping': 'identity',
        'original_char_to_byte': original_boundaries,
        'rendered_char_to_byte': rendered_boundaries,
        'comments': [{'characters': [a, b], 'source_bytes': [original_boundaries[a], original_boundaries[b]],
                      'rendered_bytes': [rendered_boundaries[a], rendered_boundaries[b]]} for a, b in comment_chars],
        'executable_token_characters': [list(span) for span in token_chars]},
        'provenance': {'policy': policy, 'language': language, 'grammar': evidence_ast.grammar_signature(),
                       'source_sha256': _sha(source), 'rendered_sha256': _sha(rendered), 'wrapper': wrapper,
                       'executable_tokens_verified': True, 'ast_structure_verified': True,
                       'equivalence_scope': 'parser_tokens_and_ast_only',
                       'toolchain_behavior_verified': False,
                       'directive_sensitive_comments': 'excluded_known_forms'}}
    if preserve_istanbul_comments:
        result['provenance'].update(preserve_istanbul_comments=True, preserved_istanbul_comments=preserved,
            directive_sensitive_comments='canonical_istanbul_preserved_other_known_forms_excluded')
    return result


def render_input(target, items, *, preserve_istanbul_comments=False):
    """Copy already selected input. Never add, recover, reorder or resize items."""
    policy = _policy(preserve_istanbul_comments=preserve_istanbul_comments)
    if not isinstance(target, dict) or not isinstance(items, list):
        raise RenderingError('input_shape_unsupported')
    if (set(target) - {'code', 'path', 'language', 'symbol'} or any(
            not isinstance(item, dict) or set(item) - {'kind', 'path', 'name', 'text'} for item in items)):
        raise RenderingError('privileged_or_unknown_input_field')
    output_target, output_items = copy.deepcopy(target), copy.deepcopy(items)
    target_source = render_code(target.get('code'), target.get('language'),
                                preserve_istanbul_comments=preserve_istanbul_comments)
    output_target['code'] = target_source['rendered']
    item_sources = []
    for item in output_items:
        if item.get('kind') in CODE_KINDS:
            source = render_code(item.get('text'), target.get('language'),
                                 preserve_istanbul_comments=preserve_istanbul_comments)
            item['text'] = source['rendered']
            item_sources.append(source)
        elif item.get('kind') in UNCHANGED_KINDS:
            item_sources.append(None)
        else:
            raise RenderingError('selected_item_kind_unsupported')
    return {'target': output_target, 'items': output_items,
            'sources': {'target': target_source, 'items': item_sources},
            'provenance': {'policy': policy, 'budget_reused': False, 'item_order_preserved': True}}


def assert_identifier_free(rendered, extra=None):
    """Final exclusion across shown code, non-code text and metadata channels.

    This isolated assertion supplements the unchanged production context gate.
    Provenance and coordinate maps are intentionally never student input.
    """
    def values(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for key, item in value.items():
                yield from values(key)
                yield from values(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from values(item)
    if any(IDENTIFIER.search(value) for value in values(
            [rendered['target'], rendered['items'], extra or {}])):
        raise RenderingError('answer_bearing_source_identifier')


def map_quote(source, rendered, quote, *, preserve_istanbul_comments=False):
    """First establish exact original ownership, then map that same interval."""
    policy = _policy(preserve_istanbul_comments=preserve_istanbul_comments)
    if _sha(source) != rendered['provenance']['source_sha256']:
        raise RenderingError('quote_source_mismatch')
    # A legacy or caller-supplied token-span list is not ownership evidence.
    # Revalidate the bounded original source, including unsupported AST kinds,
    # directive exclusions and the current coordinate/provenance policy.
    checked = render_code(source, rendered['provenance'].get('language'),
                          preserve_istanbul_comments=preserve_istanbul_comments)
    if checked != rendered:
        raise RenderingError('quote_rendering_mismatch')
    shown = rendered['rendered']
    if _sha(shown) != rendered['provenance']['rendered_sha256']:
        raise RenderingError('quote_rendering_mismatch')
    if not isinstance(quote, str) or not quote.strip():
        raise RenderingError('quote_not_owned')
    start = source.find(quote)
    if start < 0:
        raise RenderingError('quote_not_owned')
    if source.find(quote, start + 1) >= 0:
        raise RenderingError('quote_ambiguous')
    end = start + len(quote)
    mapping = rendered['source_map']
    if not any(start <= a < b <= end for a, b in mapping['executable_token_characters']):
        raise RenderingError('quote_comment_only')
    old_bytes, new_bytes = mapping['original_char_to_byte'], mapping['rendered_char_to_byte']
    return {'quote': shown[start:end],
            'source_span': {'characters': [start, end], 'bytes': [old_bytes[start], old_bytes[end]]},
            'rendered_span': {'characters': [start, end], 'bytes': [new_bytes[start], new_bytes[end]]},
            'rendering_policy': policy}
