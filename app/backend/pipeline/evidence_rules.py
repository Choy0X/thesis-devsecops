"""Small source proofs with explicit accepted syntax and limited claims.

These are not arbitrary-program vulnerability detectors. Every unrecognised
control flow or dependency stays outside a rule instead of acquiring a label.
"""
from __future__ import annotations

import json
import functools
import re

from . import evidence_ast

VERSION = 'mechanism-rules/5'

# Pinned collected source forms. Unknown siblings or changed operations stay unsupported.
_QUERY_OBJECT = r'''var parseObject = function (chain, val, options) {
    var leaf = val;

    for (var i = chain.length - 1; i >= 0; --i) {
        var obj;
        var root = chain[i];

        if (root === '[]' && options.parseArrays) {
            obj = [].concat(leaf);
        } else {
            obj = options.plainObjects ? Object.create(null) : {};
            var cleanRoot = root.charAt(0) === '[' && root.charAt(root.length - 1) === ']' ? root.slice(1, -1) : root;
            var index = parseInt(cleanRoot, 10);
            if (!options.parseArrays && cleanRoot === '') {
                obj = { 0: leaf };
            } else if (
                !isNaN(index)
                && root !== cleanRoot
                && String(index) === cleanRoot
                && index >= 0
                && (options.parseArrays && index <= options.arrayLimit)
            ) {
                obj = [];
                obj[index] = leaf;
            } else {
                obj[cleanRoot] = leaf;
            }
        }

        leaf = obj;
    }

    return leaf;
};'''
_ASSEMBLER_VALUE = r'''_saveValue(value) {
    if (this.done) {
      this.current = value;
      return;
    }
    if (this.current instanceof Array) {
      this.current.push(value);
    } else {
      this.current[this.key] = value;
      this.key = null;
    }
  }'''
_ASSEMBLER_FIXED = r'''_saveValue(value) {
    if (this.done) {
      this.current = value;
      return;
    }
    if (this.current instanceof Array) {
      this.current.push(value);
    } else {
      // like JSON.parse: an own property, never the inherited __proto__ setter
      if (this.key === '__proto__') {
        Object.defineProperty(this.current, this.key, {value, writable: true, enumerable: true, configurable: true});
      } else {
        this.current[this.key] = value;
      }
      this.key = null;
    }
  }'''


# These are intentionally complete programs, not substring signatures. A rule
# accepts only this control flow, these operations, and consistent injective
# renaming of the stated lexical bindings. Unrecognised siblings, calls,
# assignments and branches cannot inherit the proof from a matching sink.
_TOKEN_MAP = '''var onfieldoptionvalue = function (tokens) {
  var value = tokens.shift()
  if (value !== '{') {
    return value
  }
  value = {}
  var field = ''
  while (tokens.length) {
    switch (tokens[0]) {
      case '}':
        tokens.shift()
        return value
      case ':':
        tokens.shift()
        value[field] = onfieldoptionvalue(tokens)
        break
      default:
        field = tokens.shift()
    }
  }
}'''

_GIT_MERGE_BASE = '''function gitMergeBase(input: { base: string; head: string }) {
  try {
    return execSync(`git merge-base ${input.head} ${input.base}`)
      .toString()
      .trim();
  } catch (error) {
    if (
      checkIsExecError(error) &&
      error.status === 1 &&
      error.stderr.toString() === ""
    ) {
      return null;
    }
    throw error;
  }
}'''

_OBJECT_RECURSION = '''function deepMerge<T>(defaults: T, overrides: DeepPartial<T>): void {
  Object.keys(overrides).forEach((key) => {
    const defaultValue = (defaults as any)[key];
    const overrideValue = (overrides as any)[key];
    if (overrideValue !== undefined) {
      if (defaultValue === undefined || typeof defaultValue !== 'object' || typeof overrideValue !== 'object' || Array.isArray(defaultValue)) {
        (defaults as any)[key] = overrideValue;
      } else {
        deepMerge(defaultValue, overrideValue as DeepPartial<T>);
      }
    }
  });
}'''

_ROOT_RESOLVER = '''ret.resolve = function (to, from) {
    if (basepath) {
      from = basepath;
    } else {
      from = (from) ? path.dirname(from) : process.cwd();
    }
    return path.resolve(from, to);
  };'''
_ROOT_RESOLVER_FIXED = '''ret.resolve = function (to, from) {
    var resolved, rootWithSep;
    if (basepath) {
      from = basepath;
    } else {
      from = (from) ? path.dirname(from) : process.cwd();
    }
    resolved = path.resolve(from, to);
    if (basepath && !allowOutsideRoot) {
      rootWithSep = path.normalize(basepath + path.sep);
      if (resolved !== basepath && resolved.indexOf(rootWithSep) !== 0) {
        throw new Error('Template "' + to + '" resolves outside the loader root "' + basepath + '" (CVE-2023-25345).');
      }
    }
    return resolved;
  };'''


@functools.lru_cache(maxsize=16)
def _parsed_template(template, language):
    return evidence_ast.parse(language, template)


def _template_bindings(parsed, template, language, slots, bindings=None):
    """Match the entire AST, allowing only alpha-renaming of known bindings."""
    model = _parsed_template(template, language)
    if model is None:
        return None
    candidate, data = parsed
    expected, reference = model
    result = dict(bindings or {})
    reserved = {text(n, reference) for n in walk(expected)
                if n.type == 'identifier' and text(n, reference) not in slots}

    def match(actual, pattern):
        if actual.type != pattern.type:
            return False
        if pattern.type == 'identifier' and text(pattern, reference) in slots:
            slot, name = text(pattern, reference), text(actual, data)
            if not re.fullmatch(r'[A-Za-z_$][A-Za-z0-9_$]*',name) or name in reserved:
                return False
            if slot in result:
                return result[slot] == name
            if name in result.values():
                return False
            result[slot] = name
            return True
        actual_children = [n for n in actual.children if n.type != 'comment']
        expected_children = [n for n in pattern.children if n.type != 'comment']
        if len(actual_children) != len(expected_children):
            return False
        if not expected_children:
            return text(actual, data) == text(pattern, reference)
        return all(match(a, b) for a, b in zip(actual_children, expected_children))

    return result if match(candidate, expected) else None


def _has_cwe(row, required):
    values = row.get('cwe_ids')
    if isinstance(values, str):
        try:
            values = json.loads(values)
        except ValueError:
            values = [values]
    return isinstance(values, (list, tuple, set)) and required in values


def _recursive_token_dictionary(row, before, after):
    if not _has_cwe(row, 'CWE-1321'):
        return None
    slots = {'onfieldoptionvalue', 'tokens', 'value', 'field'}
    bindings = _template_bindings(before, _TOKEN_MAP, 'javascript', slots)
    if bindings is None or 'Object' in bindings.values():
        return None
    fixed = _TOKEN_MAP.replace('value = {}', 'value = Object.create(null)')
    if _template_bindings(after, fixed, 'javascript', slots, bindings) is None:
        return None
    br, bd = before; ar, ad = after
    write = next(n for n in walk(br) if n.type == 'assignment_expression'
                 and n.child_by_field_name('left').type == 'subscript_expression')
    allocation = next(n for n in walk(ar) if n.type == 'call_expression'
                      and text(n.child_by_field_name('function'), ad) == 'Object.create')
    scope = ('Prototype-changing property writes while parsing this recursive token dictionary. '
             'The returned map is an own-property record; inherited Object methods and downstream uses are outside the map contract.')
    return {
        'method': 'static_closed_world', 'mechanism': 'prototype_pollution',
        'rule_id': 'recursive-token-dictionary/1',
        'violated_invariant': 'A parsed dictionary key must create an own entry rather than invoke the inherited __proto__ setter.',
        'attacker_control': f'The supplied {bindings["tokens"]} array supplies dictionary keys and recursively parsed values.',
        'preconditions': ['A finite plain array of string tokens contains a well-formed dictionary with a nested dictionary value.',
                          'The token grammar uses { and } as dictionary delimiters and : before a value.',
                          'Standard Array.shift and Object.create intrinsics apply; getters, proxies and modified intrinsics are outside this proof.'],
        'evidence_spans': [{'quote': text(write, bd)}], 'condition_scope': scope,
        'supported_impact': 'A nested dictionary assigned to __proto__ changes the returned dictionary prototype. Global Object.prototype pollution is not established.',
        'behavioral_intent': 'Parse the same ordinary own dictionary entries, including nested maps, without inherited prototype setters.',
        'fixed_review': {'scope': scope,
                         'safeguard': 'Every recursive dictionary is allocated with a null prototype before any computed property write.',
                         'evidence': [{'quote': text(allocation, ad)}]},
        'repair_valid': True,
    }


def _builtin_import(row, side, required):
    """Require a syntactic immutable ES binding in source-bound import context."""
    source_path = row.get('file_path_' + side)
    if not isinstance(source_path, str) or not source_path:
        return False
    context = row.get('context_' + side)
    if isinstance(context, str):
        try:
            context = json.loads(context)
        except (TypeError, ValueError):
            return False
    if not isinstance(context, dict) or not isinstance(context.get('items'), list):
        return False
    found = []
    for item in context['items']:
        if (not isinstance(item, dict) or item.get('kind') != 'imports'
                or item.get('path') != source_path):
            continue
        parsed = evidence_ast.parse('typescript', item.get('text'))
        if parsed is None:
            return False
        root, data = parsed
        for statement in children(root):
            if statement.type != 'import_statement':
                return False
            module = text(statement.child_by_field_name('source'), data)
            statement_type_only = any(n.type == 'type' for n in statement.children)
            for clause in children(statement):
                if clause.type != 'import_clause':
                    continue
                for binding in children(clause):
                    if binding.type == 'identifier' and text(binding, data) == required:
                        found.append(False)  # A default import is a different binding.
                    if binding.type == 'namespace_import' and any(text(n, data) == required for n in children(binding)):
                        found.append(False)
            for node in walk(statement):
                if node.type != 'import_specifier':
                    continue
                name = text(node.child_by_field_name('name'), data)
                alias = node.child_by_field_name('alias')
                local = text(alias, data) if alias is not None else name
                if local == required:
                    found.append(module in ('"node:child_process"', "'node:child_process'",
                                            '"child_process"', "'child_process'")
                                 and name == required and alias is None and not statement_type_only
                                 and not any(n.type == 'type' for n in node.children))
    return found == [True]


def _git_merge_base_shell(row, before, after):
    if not _has_cwe(row, 'CWE-78'):
        return None
    slots = {'gitMergeBase', 'input', 'error'}
    bindings = _template_bindings(before, _GIT_MERGE_BASE, 'typescript', slots)
    if bindings is None or set(bindings.values()) & {'execSync', 'execFileSync'}:
        return None
    fixed = _GIT_MERGE_BASE.replace('execSync(`git merge-base ${input.head} ${input.base}`)',
                                   'execFileSync("git", ["merge-base", input.head, input.base])')
    if _template_bindings(after, fixed, 'typescript', slots, bindings) is None:
        return None
    if not _builtin_import(row, 'before', 'execSync') or not _builtin_import(row, 'after', 'execFileSync'):
        return None
    br, bd = before; ar, ad = after
    old = next(n for n in walk(br) if n.type == 'call_expression'
               and text(n.child_by_field_name('function'), bd) == 'execSync')
    new = next(n for n in walk(ar) if n.type == 'call_expression'
               and text(n.child_by_field_name('function'), ad) == 'execFileSync')
    scope = ('Shell interpretation of the supplied Git merge-base arguments in this operation. '
             'Git option handling, repository policy and the external error classifier are outside this scope.')
    return {
        'method': 'static_closed_world', 'mechanism': 'command_injection',
        'rule_id': 'git-merge-base-shell-argv/1',
        'violated_invariant': 'Untrusted revision strings must not be interpreted as shell command syntax.',
        'attacker_control': f'The supplied {bindings["input"]}.head and {bindings["input"]}.base string fields enter the command.',
        'preconditions': ['The supplied revision fields are plain string data properties on a non-proxy object.',
                          'Node child_process uses its standard shell execution behavior and the Git executable is available.',
                          'For benign equivalence, revision arguments are ordinary Git revisions without shell metacharacters or whitespace.'],
        'evidence_spans': [{'quote': text(old, bd)}], 'condition_scope': scope,
        'supported_impact': 'Shell command substitution or shell operators in a supplied revision can execute commands before Git runs.',
        'behavioral_intent': 'Invoke git merge-base with the same ordinary revision arguments and existing return/error handling.',
        'fixed_review': {'scope': scope,
                         'safeguard': 'The immutable child_process.execFileSync binding receives a literal executable and a separate argument array, with no shell option.',
                         'evidence': [{'quote': text(new, ad)}]},
        'repair_valid': True,
    }


def walk(node):
    yield node
    for child in node.named_children:
        yield from walk(child)


def text(node, data):
    return data[node.start_byte:node.end_byte].decode() if node is not None else ''


def children(node):
    return [x for x in node.named_children if x.type != 'comment'] if node is not None else []


def shape(node, data, omit=None):
    if node.type == 'comment' or (omit is not None and node.id == omit.id):
        return None
    values = [value for child in node.children if (value := shape(child, data, omit)) is not None]
    return (node.type, tuple(values)) if values else (node.type, text(node, data))


def _operator(node):
    return next((c.type for c in node.children if not c.is_named and c.type not in ('(', ')')), '')


def _guard_keys(node, data, key):
    if node.type == 'parenthesized_expression' and len(children(node)) == 1:
        return _guard_keys(children(node)[0], data, key)
    if node.type != 'binary_expression':
        return set()
    left, right = node.child_by_field_name('left'), node.child_by_field_name('right')
    op = _operator(node)
    if op == '||':
        a, b = _guard_keys(left, data, key), _guard_keys(right, data, key)
        return a | b if a and b else set()
    if op in ('==', '==='):
        for name, literal in ((left, right), (right, left)):
            raw = text(literal, data)
            if (name.type == 'identifier' and text(name, data) == key and literal.type == 'string'
                    and len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'")
                    and raw[1:-1] in ('__proto__', 'constructor', 'prototype')):
                return {raw[1:-1]}
    return set()


def _shallow_copy(row, before, after):
    if not _has_cwe(row, 'CWE-1321'):
        return None
    br, bd = before; ar, ad = after
    functions = [n for n in walk(br) if n.type in ('function_declaration', 'function_expression', 'arrow_function')]
    if len(functions) != 1:
        return None
    function = functions[0]
    envelope = function
    while envelope.parent is not None and envelope.parent.type in (
            'export_statement', 'expression_statement', 'parenthesized_expression'):
        envelope = envelope.parent
        if len(children(envelope)) != 1:
            return None
    if envelope.parent != br or children(br) != [envelope]:
        return None
    parameters = function.child_by_field_name('parameters')
    if parameters is None or any(x.type != 'identifier' for x in children(parameters)):
        return None
    params = [text(x, bd) for x in children(parameters)]
    if not params or len(params) != len(set(params)):
        return None
    # No getters/callbacks, hidden predicates, calls, nested functions or branches
    # participate in this particular shallow-copy proof.
    if any(n.type in ('call_expression', 'new_expression', 'if_statement', 'while_statement', 'do_statement',
                      'switch_statement', 'try_statement', 'conditional_expression') for n in walk(br)):
        return None
    assignments = [n for n in walk(br) if n.type == 'assignment_expression']
    if len(assignments) != 1:
        return None
    assignment = assignments[0]
    left, right = assignment.child_by_field_name('left'), assignment.child_by_field_name('right')
    if left.type != 'subscript_expression' or right.type != 'subscript_expression' or _operator(assignment) != '=':
        return None
    destination, key_node = left.child_by_field_name('object'), left.child_by_field_name('index')
    source_node, source_key = right.child_by_field_name('object'), right.child_by_field_name('index')
    if any(n.type != 'identifier' for n in (destination, key_node, source_node, source_key)):
        return None
    target, key, source = (text(n, bd) for n in (destination, key_node, source_node))
    if target != params[0] or len({target, key, source}) != 3 or text(source_key, bd) != key or key in params:
        return None
    loops = [n for n in walk(br) if n.type == 'for_in_statement']
    if len(loops) != 1:
        return None
    loop = loops[0]
    # The grammar uses the same node type for both property enumeration and
    # iterable for-of. Only in enumerates the plain object's property keys.
    if (not any(n.type in ('var', 'let', 'const') for n in loop.children)
            or not any(n.type == 'in' for n in loop.children)):
        return None
    if text(loop.child_by_field_name('left'), bd) != key or text(loop.child_by_field_name('right'), bd) != source:
        return None
    body = children(loop.child_by_field_name('body'))
    if len(body) != 1 or body[0].type != 'expression_statement' or children(body[0]) != [assignment]:
        return None
    statements = children(function.child_by_field_name('body'))
    if len(statements) != 2 or statements[-1].type != 'return_statement' or text(children(statements[-1])[0], bd) != target:
        return None
    declarations = [n for n in walk(br) if n.type == 'variable_declarator']
    names = [text(n.child_by_field_name('name'), bd) for n in declarations]
    if len(names) != len(set(names)) or set(names) & set(params):
        return None
    if statements[0] == loop:
        if source not in params or declarations:
            return None
    else:
        outer = statements[0]
        if outer.type != 'for_statement' or function.type == 'arrow_function' or 'arguments' in params:
            return None
        outer_body = children(outer.child_by_field_name('body'))
        if len(outer_body) != 2 or outer_body[1] != loop or len(declarations) != 2:
            return None
        initial = outer.child_by_field_name('initializer')
        if initial is None or len(children(initial)) != 1:
            return None
        variable = children(initial)[0]
        index = text(variable.child_by_field_name('name'), bd)
        if (variable.type != 'variable_declarator' or text(variable.child_by_field_name('value'), bd) != '1'
                or index in params or index in (key, source)):
            return None
        compact = lambda n: ''.join(text(n, bd).split())
        if (compact(outer.child_by_field_name('condition')) != index + '<arguments.length'
                or compact(outer.child_by_field_name('increment')) not in (index + '++', '++' + index)):
            return None
        values = children(outer_body[0])
        if len(values) != 1 or values[0].type != 'variable_declarator':
            return None
        if (text(values[0].child_by_field_name('name'), bd) != source
                or compact(values[0].child_by_field_name('value')) != 'arguments[' + index + ']'):
            return None
    guards = [n for n in walk(ar) if n.type == 'if_statement']
    if len(guards) != 1:
        return None
    guard = guards[0]
    if guard.child_by_field_name('alternative') is not None:
        return None
    action = guard.child_by_field_name('consequence')
    if action is not None and action.type == 'statement_block' and len(children(action)) == 1:
        action = children(action)[0]
    if action is None or action.type != 'continue_statement' or children(action):
        return None
    if '__proto__' not in _guard_keys(guard.child_by_field_name('condition'), ad, key):
        return None
    parent_body = guard.parent
    if parent_body.type != 'statement_block' or children(parent_body)[0] != guard:
        return None
    after_loop = parent_body.parent
    if (after_loop.type != 'for_in_statement' or not any(n.type == 'in' for n in after_loop.children)
            or text(after_loop.child_by_field_name('left'), ad) != key):
        return None
    if shape(br, bd) != shape(ar, ad, guard):
        return None
    scope = 'Prototype-changing __proto__ assignment in this shallow property-copy operation; recursive merges and downstream uses are outside this scope.'
    invariant = 'An untrusted __proto__ property name must not reach a write to an ordinary destination object.'
    return {'method': 'static_closed_world', 'mechanism': 'prototype_pollution', 'rule_id': 'shallow-prototype-write/1',
            'violated_invariant': invariant, 'attacker_control': f'Properties of the supplied {source} object select {key}.',
            'preconditions': ['An untrusted plain data object supplies an enumerable own __proto__ property.',
                              f'{target} is a separate ordinary destination object with the standard JavaScript prototype setter.',
                              'The supplied source uses plain data properties; getters and modified JavaScript intrinsics are outside this proof.'],
            'evidence_spans': [{'quote': text(assignment, bd)}], 'condition_scope': scope,
            'supported_impact': 'The destination object prototype can change. Global Object.prototype pollution is not established by this shallow copy.',
            'behavioral_intent': 'Copy ordinary properties while refusing prototype-changing __proto__ assignments.',
            'fixed_review': {'scope': scope, 'safeguard': 'The loop skips __proto__ before every shallow property write.',
                             'evidence': [{'quote': text(guard, ad)}]}, 'repair_valid': True}


def _query_dictionary(row, before, after):
    if not _has_cwe(row, 'CWE-1321'):
        return None
    slots = {'parseObject', 'chain', 'val', 'options', 'leaf', 'i', 'obj', 'root', 'cleanRoot', 'index'}
    bindings = _template_bindings(before, _QUERY_OBJECT, 'javascript', slots)
    if bindings is None:
        return None
    fixed = _QUERY_OBJECT.replace("} else {\n                obj[cleanRoot] = leaf;",
                                  "} else if (cleanRoot !== '__proto__') {\n                obj[cleanRoot] = leaf;")
    if fixed == _QUERY_OBJECT or _template_bindings(after, fixed, 'javascript', slots, bindings) is None:
        return None
    _, bd = before
    ar, ad = after
    guard = next(n for n in walk(ar) if n.type == 'binary_expression'
                 and text(n.child_by_field_name('left'), ad) == bindings['cleanRoot']
                 and text(n.child_by_field_name('right'), ad) == "'__proto__'")
    write = next(n for n in walk(before[0]) if n.type == 'assignment_expression'
                 and text(n.child_by_field_name('left'), bd) == bindings['obj'] + '[' + bindings['cleanRoot'] + ']')
    scope = 'Prototype-changing __proto__ writes while constructing these fresh query dictionaries; downstream merges and uses are outside this scope.'
    return {'method': 'static_closed_world', 'mechanism': 'prototype_pollution', 'rule_id': 'fresh-query-dictionary/1',
            'violated_invariant': 'A decoded untrusted query key must not invoke the inherited prototype setter on a fresh dictionary.',
            'attacker_control': 'The supplied chain array controls each raw and bracket-decoded query key; val supplies its value.',
            'preconditions': ['chain is a finite array of primitive string keys, and val is plain JSON data.',
                              'options supplies plain data fields parseArrays, plainObjects and arrayLimit; modified intrinsics, accessors and proxies are outside this proof.',
                              'The vulnerable branch uses an ordinary dictionary (plainObjects is false) and a decoded __proto__ key whose value is an object.'],
            'evidence_spans': [{'quote': text(write, bd)}], 'condition_scope': scope,
            'supported_impact': 'An attacker-controlled value can become the prototype of the returned query dictionary. Global Object.prototype mutation is not established.',
            'behavioral_intent': 'Construct the same ordinary query properties and arrays while refusing prototype-changing decoded keys.',
            'fixed_review': {'scope': scope, 'safeguard': 'The decoded key is compared with __proto__ immediately before the only arbitrary-name write; numeric array writes use numeric indices and null-prototype dictionaries remain null-prototype.',
                             'evidence': [{'quote': text(guard, ad)}]}, 'repair_valid': True}


def _assembler_value(row):
    if row.get('language') not in ('javascript', 'js', 'typescript', 'ts') or not _has_cwe(row, 'CWE-1321'):
        return None
    source = row.get('vulnerable_code') or ''
    fixed = row.get('fixed_code') or ''
    if not isinstance(source, str) or not isinstance(fixed, str) or max(len(source), len(fixed)) > 100000:
        return None
    wrapper = lambda code: 'class Subject {\n' + code + '\n}'
    before = evidence_ast.parse('javascript', wrapper(source))
    after = evidence_ast.parse('javascript', wrapper(fixed))
    if before is None or after is None:
        return None
    # Descriptor shorthand fixes the property name and its lexical value to
    # `value`. Renaming only ordinary identifiers would leave an unbound read.
    slots = set()
    bindings = _template_bindings(before, wrapper(_ASSEMBLER_VALUE), 'javascript', slots)
    if bindings is None or _template_bindings(after, wrapper(_ASSEMBLER_FIXED), 'javascript', slots, bindings) is None:
        return None
    br, bd = before
    ar, ad = after
    write = next(n for n in walk(br) if n.type == 'assignment_expression'
                 and text(n.child_by_field_name('left'), bd) == 'this.current[this.key]')
    define = next(n for n in walk(ar) if n.type == 'call_expression'
                  and text(n.child_by_field_name('function'), ad) == 'Object.defineProperty')
    scope = 'Prototype-changing property writes in this JSON assembler operation; consumer behavior and whole-repository safety are outside this scope.'
    return {'method': 'static_closed_world', 'mechanism': 'prototype_pollution', 'rule_id': 'assembler-own-property/1',
            'violated_invariant': 'A parsed JSON property name must create an own data property rather than change the current object prototype.',
            'attacker_control': 'The parsed name in this.key selects the property; the supplied value supplies the parsed JSON value.',
            'preconditions': ['this.done is false, this.current is a separate extensible ordinary JSON object, and this.key is a primitive string from the parser.',
                              'The current object has ordinary configurable writable data properties, and the supplied value is plain JSON data.',
                              'Object.defineProperty and Array operations retain standard JavaScript semantics; getters, proxies and modified intrinsics are outside this proof.'],
            'evidence_spans': [{'quote': text(write, bd)}], 'condition_scope': scope,
            'supported_impact': 'A parsed __proto__ key and object value can change the current assembled object prototype. Global Object.prototype mutation is not established.',
            'behavioral_intent': 'Assemble the same JSON values and own properties while preserving the destination prototype.',
            'fixed_review': {'scope': scope, 'safeguard': 'The __proto__ branch uses Object.defineProperty with a writable, enumerable and configurable own data property; ordinary names keep the same assignment, array and completed-value branches.',
                             'evidence': [{'quote': text(define, ad)}]}, 'repair_valid': True}


def _object_only_recursion(row, before, after):
    if not _has_cwe(row,'CWE-1321'):
        return None
    slots = {'deepMerge','defaults','overrides','key','defaultValue','overrideValue'}
    bindings = _template_bindings(before,_OBJECT_RECURSION,'typescript',slots)
    fixed = _OBJECT_RECURSION.replace('Object.keys(overrides).forEach',
                                     "Object.keys(overrides).filter((key) => key !== '__proto__').forEach")
    if bindings is None or _template_bindings(after,fixed,'typescript',slots,bindings) is None:
        return None
    br,bd = before; ar,ad = after
    write = next(n for n in walk(br) if n.type == 'assignment_expression')
    guard = next(n for n in walk(ar) if n.type == 'binary_expression'
                 and text(n.child_by_field_name('right'),ad) == "'__proto__'")
    scope = 'Prototype pollution through own string keys in this recursive plain JSON object merge; custom prototypes, proxies, accessors and non-JSON application objects are outside this scope.'
    return {'method':'static_closed_world','mechanism':'prototype_pollution','rule_id':'object-only-recursive-merge/1',
            'violated_invariant':'Own input keys must not cause recursion into Object.prototype or invoke the inherited prototype setter.',
            'attacker_control':f'The supplied {bindings["overrides"]} object controls the own property names and JSON values.',
            'preconditions':['Both inputs are finite acyclic plain JSON data with ordinary prototypes and data properties; standard Object.keys and Array.isArray intrinsics apply.',
                             'A JSON-parsed own __proto__ entry with a nested object is supplied to the merge.',
                             'No preexisting aliases to global prototypes are stored in own destination data properties.'],
            'evidence_spans':[{'quote':text(write,bd)}],'condition_scope':scope,
            'supported_impact':'The vulnerable merge follows the inherited __proto__ object and can write attacker-selected properties into Object.prototype.',
            'behavioral_intent':'Merge ordinary own JSON data recursively without changing global prototype state.',
            'fixed_review':{'scope':scope,'safeguard':'Object.keys produces primitive string keys and the filter excludes __proto__ before every read, recursion and write. Function-valued inherited constructor and Object methods take the assignment branch rather than being recursively mutated.',
                            'evidence':[{'quote':text(guard,ad)}]},'repair_valid':True}


def _path_namespace(row,side):
    try:
        context = json.loads(row.get('context_'+side)) if isinstance(row.get('context_'+side),str) else row.get('context_'+side) or {}
    except ValueError:
        return False
    matches = []
    for item in context.get('items',[]):
        if item.get('kind') != 'imports' or item.get('path') != row.get('file_path_'+side):
            continue
        parsed = evidence_ast.parse('javascript',item.get('text'))
        if parsed is None:
            continue
        root,data = parsed
        for statement in children(root):
            if statement.type not in ('variable_declaration','lexical_declaration'):
                continue
            for decl in children(statement):
                if decl.type != 'variable_declarator' or text(decl.child_by_field_name('name'),data) != 'path':
                    continue
                value = decl.child_by_field_name('value')
                args = children(value.child_by_field_name('arguments')) if value is not None and value.type == 'call_expression' else []
                matches.append(len(args) == 1 and text(value.child_by_field_name('function'),data) == 'require'
                               and text(args[0],data) in ("'path'",'"path"',"'node:path'",'"node:path"'))
    return matches == [True]


def _root_resolver(row,before,after):
    if not _has_cwe(row,'CWE-22'):
        return None
    if (_template_bindings(before,_ROOT_RESOLVER,'javascript',set()) is None
            or _template_bindings(after,_ROOT_RESOLVER_FIXED,'javascript',set()) is None
            or not all(_path_namespace(row,side) for side in ('before','after'))):
        return None
    br,bd=before;ar,ad=after
    resolve=next(n for n in walk(br) if n.type=='call_expression' and text(n.child_by_field_name('function'),bd)=='path.resolve')
    guard=next(n for n in walk(ar) if n.type=='if_statement' and 'rootWithSep' in text(n.child_by_field_name('condition'),ad))
    scope='Lexical POSIX path containment under the configured absolute template root with allowOutsideRoot disabled. Symlink resolution and filesystem permissions are outside this scope.'
    return {'method':'static_closed_world','mechanism':'path_traversal','rule_id':'resolved-root-separator/1',
            'violated_invariant':'An untrusted template name must not resolve outside the configured template root.',
            'attacker_control':'The supplied to string controls absolute or relative template path components.',
            'preconditions':['basepath is a trusted normalized absolute POSIX directory other than /, without a trailing separator; allowOutsideRoot is false.',
                             'to is an untrusted primitive string; the source-bound Node path binding and standard path/process intrinsics are unchanged.',
                             'Containment is lexical. No symlink or filesystem safety claim is made.'],
            'evidence_spans':[{'quote':text(resolve,bd)}],'condition_scope':scope,
            'supported_impact':'A ../ path or an absolute path can select a template outside the configured root.',
            'behavioral_intent':'Resolve benign template paths inside the same root while rejecting absolute and sibling-prefix escapes.',
            'fixed_review':{'scope':scope,'safeguard':'The resolved absolute path must equal the root or start with its normalized separator-terminated prefix. The separator prevents sibling-prefix bypasses.',
                            'evidence':[{'quote':text(guard,ad)}]},'repair_valid':True}


def _path_domain_containment(row):
    """Prove a separator repair by interpreting a complete bounded path unit.

    The four domains partition every primitive resolved POSIX path under the
    stated normalized-root contract. Boolean guards are interpreted exactly;
    this does not infer a fix from an identifier, guard substring or test case.
    """
    if not _has_cwe(row, 'CWE-22'):
        return None
    from . import evidence_shell
    parsed = [evidence_shell._parse_unit(row.get('language'), row.get(key))
              for key in ('vulnerable_code', 'fixed_code')]
    if not all(parsed):
        return None
    domains = ('equal', 'inside', 'sibling', 'outside')
    expected = ((False, False, False, True), (False, False, True, True))
    summaries = []
    for side, (tree, data), rejects in zip(('before', 'after'), parsed, expected):
        units, pending, visited = [], [(tree, 0)], 0
        while pending:
            node, depth = pending.pop()
            visited += 1
            if visited > 2000 or depth > 64:
                return None
            if node.type in ('function_declaration', 'function_expression', 'arrow_function', 'method_definition'):
                units.append(node)
            pending.extend((child, depth + 1) for child in node.named_children)
        if len(units) != 1:
            return None
        unit = units[0]
        if not evidence_shell._unit_owner(unit):
            return None
        if unit.type == 'method_definition':
            # The parser-generated class is a syntax wrapper only. A supplied
            # class can execute extends/static code and is not a selected unit.
            if evidence_ast.parse(row.get('language'), row.get('vulnerable_code' if side == 'before' else 'fixed_code')) is not None:
                return None
        else:
            parent = unit.parent
            while parent is not None:
                if parent.type == 'assignment_expression' or len(children(parent)) != 1:
                    return None
                parent = parent.parent
        if any(c.type in ('async', '*', 'get', 'set') for c in unit.children):
            return None
        parameters = evidence_shell._parameters(unit, data)
        statements = children(unit.child_by_field_name('body'))
        if len(parameters) != 1 or len(statements) != 5:
            return None
        parameter = next(iter(parameters))
        bindings = []
        for statement in statements[:3]:
            declarations = children(statement)
            if (statement.type != 'lexical_declaration' or not any(c.type == 'const' for c in statement.children)
                    or len(declarations) != 1 or declarations[0].type != 'variable_declarator'):
                return None
            declaration = declarations[0]
            name, value = declaration.child_by_field_name('name'), declaration.child_by_field_name('value')
            if name is None or name.type != 'identifier' or value is None:
                return None
            bindings.append((text(name, data), value))
        local_names = [name for name, _ in bindings]
        reserved = {'normalize', 'isAbsolute', 'join', 'resolve', 'sep'}
        if len(set(local_names + [parameter])) != 4 or set(local_names + [parameter]) & reserved:
            return None
        def identifier(node, name):
            return node is not None and node.type == 'identifier' and text(node, data) == name
        def call(node, name, count):
            if node is None or node.type != 'call_expression' or not identifier(node.child_by_field_name('function'), name):
                return None
            args = children(node.child_by_field_name('arguments'))
            return args if len(args) == count else None
        normal_name, normal_value = bindings[0]
        args = call(normal_value, 'normalize', 1)
        if args is None or not identifier(args[0], parameter):
            return None
        absolute_name, absolute_value = bindings[1]
        if absolute_value.type != 'ternary_expression':
            return None
        args = call(absolute_value.child_by_field_name('condition'), 'isAbsolute', 1)
        if (args is None or not identifier(args[0], normal_name)
                or not identifier(absolute_value.child_by_field_name('consequence'), normal_name)):
            return None
        args = call(absolute_value.child_by_field_name('alternative'), 'join', 2)
        if args is None or not identifier(args[1], normal_name):
            return None
        root = args[0]
        # A plain receiver/config path is a contract input, never an evaluated
        # getter, computed property, invocation or an attacker argument alias.
        base = root
        while base.type == 'member_expression':
            prop = base.child_by_field_name('property')
            if prop is None or prop.type != 'property_identifier' or any(c.type == 'optional_chain' for c in base.children):
                return None
            base = base.child_by_field_name('object')
        if base.type != 'this' or root.type != 'member_expression':
            return None
        result_name, result_value = bindings[2]
        args = call(result_value, 'resolve', 1)
        if args is None or not identifier(args[0], absolute_name):
            return None
        guard, returned = statements[3:]
        if (guard.type != 'if_statement' or guard.child_by_field_name('alternative') is not None
                or returned.type != 'return_statement' or len(children(returned)) != 1
                or not identifier(children(returned)[0], result_name)):
            return None
        consequence = guard.child_by_field_name('consequence')
        if consequence is not None and consequence.type == 'statement_block':
            contents = children(consequence)
            consequence = contents[0] if len(contents) == 1 else None
        if (consequence is None or consequence.type != 'return_statement'
                or len(children(consequence)) != 1 or children(consequence)[0].type != 'null'):
            return None
        condition = guard.child_by_field_name('condition')
        root_shape = shape(root, data)
        def is_root(node):
            return node is not None and shape(node, data) == root_shape
        def evaluate(node, domain):
            if node is None:
                raise ValueError('missing guard')
            if node.type == 'parenthesized_expression' and len(children(node)) == 1:
                return evaluate(children(node)[0], domain)
            if node.type == 'unary_expression' and _operator(node) == '!':
                return not evaluate(node.child_by_field_name('argument'), domain)
            if node.type == 'binary_expression':
                left, right = node.child_by_field_name('left'), node.child_by_field_name('right')
                operator = _operator(node)
                if operator in ('&&', '||'):
                    # Both sides are always analyzed, including unreachable arms.
                    a, b = evaluate(left, domain), evaluate(right, domain)
                    return a and b if operator == '&&' else a or b
                if operator in ('===', '!==') and ((identifier(left, result_name) and is_root(right))
                        or (is_root(left) and identifier(right, result_name))):
                    return (domain == 'equal') == (operator == '===')
            if node.type == 'call_expression':
                function = node.child_by_field_name('function')
                args = children(node.child_by_field_name('arguments'))
                if (function is not None and function.type == 'member_expression'
                        and identifier(function.child_by_field_name('object'), result_name)
                        and text(function.child_by_field_name('property'), data) == 'startsWith'
                        and not any(c.type == 'optional_chain' for c in function.children) and len(args) == 1):
                    if is_root(args[0]):
                        return domain != 'outside'
                    arg = args[0]
                    if (arg.type == 'binary_expression' and _operator(arg) == '+'
                            and is_root(arg.child_by_field_name('left'))
                            and identifier(arg.child_by_field_name('right'), 'sep')):
                        return domain == 'inside'
            raise ValueError('unsupported guard operation')
        try:
            if tuple(evaluate(condition, domain) for domain in domains) != rejects:
                return None
        except ValueError:
            return None
        needed = reserved - ({'sep'} if side == 'before' else set())
        if not all(evidence_shell._binding(row, side, name, name, module='path') for name in needed):
            return None
        summaries.append((shape(tree, data, condition), text(guard, data), text(root, data)))
    if summaries[0][0] != summaries[1][0] or summaries[0][2] != summaries[1][2]:
        return None
    scope = ('Lexical POSIX containment at the returned resolved path under the trusted configured root. '
             'Filesystem access, symlink resolution and policy outside this method are outside this scope.')
    return {'method': 'static_closed_world', 'mechanism': 'path_traversal',
        'rule_id': 'resolved-path-domain-containment/1', 'repair_valid': True,
        'violated_invariant': 'An untrusted path must not resolve to a sibling outside the configured root.',
        'attacker_control': 'The sole path parameter supplies untrusted primitive path text.',
        'preconditions': ['The receiver and its nested configuration are stable plain data objects without accessors or proxies; ' + summaries[0][2] + ' is a trusted normalized absolute POSIX directory other than /, without a trailing separator.',
                          'Node path imports, primitive string methods and intrinsics retain standard semantics.'],
        'condition_scope': scope,
        'supported_impact': 'A sibling path sharing the root text prefix passes the original unanchored prefix check.',
        'behavioral_intent': 'Preserve normalized root and descendant paths while rejecting every resolved path outside the root.',
        'evidence_spans': [{'quote': summaries[0][1]}],
        'fixed_review': {'scope': scope,
            'safeguard': 'The complete guard rejects exactly the sibling-prefix and outside-prefix domains. Equality and separator-anchored descendants are preserved; all normalization and return dataflow is unchanged.',
            'evidence': [{'quote': summaries[1][1]}]}}


def examine(row):
    from . import evidence_shell,evidence_recursive
    if not (_has_cwe(row, 'CWE-1321') or _has_cwe(row, 'CWE-78') or _has_cwe(row,'CWE-22')):
        return evidence_shell.examine(row)
    before = evidence_ast.parse(row.get('language'), row.get('vulnerable_code'))
    after = evidence_ast.parse(row.get('language'), row.get('fixed_code'))
    if before is None or after is None:
        return _assembler_value(row) or _path_domain_containment(row) or evidence_shell.examine(row)
    return (_shallow_copy(row, before, after)
            or _recursive_token_dictionary(row, before, after)
            or _git_merge_base_shell(row, before, after)
            or _query_dictionary(row, before, after)
            or _object_only_recursion(row,before,after)
            or evidence_recursive.examine(row,before,after)
            or _root_resolver(row,before,after)
            or _path_domain_containment(row)
            or evidence_shell.examine(row))
