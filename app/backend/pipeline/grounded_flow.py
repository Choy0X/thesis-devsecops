"""Flow-aware sink detection for JavaScript and TypeScript units (used by the grounded view builder).

A keyword table cannot tell a vulnerable call from a safe one: ``execAsync("xcrun simctl list devices")`` and ``execAsync(`cat ${file}`)`` both hold the word
``execAsync``. This module parses the unit with tree-sitter and follows a value inside the function: it starts at the function's parameters (and a few global
sources such as ``location``), moves through assignments, destructuring, concatenation, template strings and calls, stops at recognised sanitisers and at
guards that return or throw, and reports a sink of the weakness class only when a still-tainted value reaches it. It is deliberately conservative:
it looks inside one function, knows no callee bodies, and treats any validator-like name as a sanitiser, so a missing flow means "not shown", never "safe".
"""
from __future__ import annotations

import functools
import re

try:
    import tree_sitter_javascript as _tsj
    import tree_sitter_typescript as _tst
    from tree_sitter import Language, Parser
except Exception:                                         # pragma: no cover; ignored: the parsers are a declared requirement, and without them _parser() returns None
    Parser = None

SUPPORTED = ('CWE-79', 'CWE-78', 'CWE-77', 'CWE-89', 'CWE-918', 'CWE-22', 'CWE-601', 'CWE-94', 'CWE-1321', 'CWE-1333', 'CWE-502')       # CWE-502: Python only
LANGUAGES = {'javascript': 'js', 'jsx': 'js', 'typescript': 'ts', 'tsx': 'ts'}
FUNCTIONS = {'function_declaration', 'function_expression', 'function', 'arrow_function', 'method_definition', 'generator_function_declaration',
             'generator_function'}
_PARSERS = {}

_SOURCE = re.compile(r'^(?:process\.argv|(?:window\.|document\.)?location(?:\.|$)|document\.(?:cookie|referrer|URL)|window\.name)')
_SANITIZER = re.compile(r'escape|sanitiz|encode|purif|xss|clean|quote|valid|verify|assert|ensure|allow|safe|whitelist|basename|normaliz|isurl|isinteger|isnumeric|'
                        r'isalnum|^is(?:valid|safe|allowed|trusted|inside|within|public|private|local|internal|legit|permitted|secure|sanitized|ok)|'
                        r'^check(?:url|path|host|dir|file|name|origin|redirect|safe|allowed|input)', re.I)
_TYPE_PREDICATE = re.compile(r'^(?:isarray|isnan|isfinite|isnumber|isstring|isboolean|isobject|isfunction|isundefined|isnull|isplainobject|isbuffer|isdate|ismap|isset|issymbol|'
                             r'isinteger|isnil|isempty|isdefined)$', re.I)
_NOT_A_SANITIZER = re.compile(r'unescape|unsanitiz|unsafe|decode|dangerous|raw', re.I)
_NONSTRING_CALLS = {'Number', 'parseInt', 'parseFloat', 'Boolean', 'isNaN', 'isFinite', 'includes', 'startsWith', 'endsWith', 'indexOf', 'lastIndexOf',
                    'has', 'hasOwnProperty', 'test', 'random', 'now', 'floor', 'ceil', 'round', 'abs', 'min', 'max', 'isArray'}
_COMPARISONS = {'==', '===', '!=', '!==', '<', '>', '<=', '>=', 'instanceof', 'in', '-', '*', '/', '%', '**', '&', '|', '^', '<<', '>>', '>>>'}
_DANGEROUS_LITERALS = {'__proto__', 'constructor', 'prototype', '..', '../', '..\\', 'localhost', '127.0.0.1', '0.0.0.0'}
_CHECK_METHODS = {'startsWith', 'endsWith', 'includes', 'indexOf', 'test', 'match', 'has', 'hasOwnProperty', 'hasOwn', 'isAbsolute', 'every', 'some', 'exec'}
MECHANISM = {
    'CWE-79': 'Markup in that value is interpreted by the browser, so a script in it runs in the visitor\'s page.',
    'CWE-78': 'Shell syntax in that value becomes part of the command, so an attacker can run other commands.',
    'CWE-77': 'Shell syntax in that value becomes part of the command, so an attacker can run other commands.',
    'CWE-89': 'SQL syntax in that value changes the query, so an attacker can read or alter other data.',
    'CWE-22': 'Path segments such as ".." in that value can point outside the intended directory.',
    'CWE-918': 'An attacker who controls that value can point the server\'s request at internal or arbitrary hosts.',
    'CWE-601': 'An attacker who controls that value can send visitors to a site of their choice.',
    'CWE-94': 'Code in that value is evaluated, so an attacker can run it.',
    'CWE-1321': 'A property name such as "__proto__" in that value can modify Object.prototype for the whole process.',
    'CWE-1333': 'A crafted value can make the nested repetition in the pattern backtrack for a very long time.',
}
_EXEC = {'exec', 'execSync', 'execAsync', 'execPromise', 'execCommand', 'shell'}
# Project helpers that run a shell command string (electerm: `run(cmd)`, `runWinCmd(cmd)`). They are a sink only when the argument is a string the code built (template or concatenation),
# so a bare `run(task)` is not read as a shell.
_CMD_WRAPPER = re.compile(r'^(?:run|runCmd|runCommand|runWinCmd|runShell|runScript|execCmd|sh|shell[A-Z]\w*)$')
_SPAWN = {'spawn', 'spawnSync', 'execFile', 'execFileSync'}
_SQL_ANY = {'query', 'execute', 'raw', 'prepare', 'whereRaw', 'andWhere', 'queryRaw', 'literal'}
_SQL_DB = {'all', 'run', 'get', 'exec', 'where'}
_DB_RECEIVER = re.compile(r'db|sql|conn|pool|client|knex|sequelize|database|stmt|tx|model|repo', re.I)
_SSRF = {'fetch', 'got', 'request', 'needle', 'urlopen', 'goto', 'superagent', 'axios'}
_URL_FETCHER = re.compile(r'^(?:fetch|download|get|load|read|open|request|pull|crawl|probe|head)\w*(?:Url|URL|Uri|URI|Remote)\w*$|^\w+FromUrl$')
_SSRF_RECEIVER = re.compile(r'^(?:axios|http|https|got|request|needle|superagent|page|rp|undici|node-fetch|\w*client)$', re.I)
_PATH_SINKS = {'readFile', 'readFileSync', 'createReadStream', 'createWriteStream', 'writeFile', 'writeFileSync', 'appendFile', 'appendFileSync', 'unlink',
               'unlinkSync', 'rm', 'rmSync', 'readdir', 'readdirSync', 'copyFile', 'copyFileSync', 'rename', 'renameSync',
               'sendFile', 'download', 'createReadStreamSync', 'rmdir', 'rmdirSync', 'openSync'}
_PATH_RECEIVER = re.compile(r'^(?:fs|fsp|promises|fse|fsExtra|res|response|reply|ctx|\w*fs)$', re.I)
_BASE_DIR = re.compile(r'^(?:__dirname|(?:this\.)?(?:base|root|dir|dirname|directory|cwd|prefix|folder|home|basedir|rootdir|basepath|rootpath|uploaddir|publicdir)\w*)$', re.I)
_NULL_PROTO = re.compile(r'without.?proto|null.?proto|nullproto|bareobject|dictionary|createmap|nulloject', re.I)
_MERGE = {'merge', 'deepMerge', 'extend', 'deepExtend', 'defaultsDeep', 'mergeWith', 'assignDeep', 'deepAssign', 'mergeDeep'}
_REGEX_USE = {'test', 'exec', 'match', 'matchAll', 'replace', 'replaceAll', 'search', 'split'}
_NESTED_QUANTIFIER = re.compile(r'\((?:\?:)?[^()\n]*[+*][^()\n]*\)[+*{]|\(\.[+*]\\?[^()\n]*\)[+*]')


_CONFIGISH = re.compile(r'config|option|opts|setting|root|base|dir|env', re.I)


def _pick(candidates):
    """The first origin, preferring one that does not look like trusted configuration."""
    found = [c for c in candidates if c]
    return next((c for c in found if not _CONFIGISH.search(c)), found[0] if found else None)


def _parser(kind):
    if Parser is None:
        return None
    if kind not in _PARSERS:
        grammar = _tsj.language() if kind == 'js' else _tst.language_tsx() if kind == 'tsx' else _tst.language_typescript()
        _PARSERS[kind] = Parser(Language(grammar))
    return _PARSERS[kind]


def _text(node):
    return node.text.decode('utf-8', 'replace')


def _short(text, limit=60):
    text = ' '.join(text.split())
    return text if len(text) <= limit else text[:limit - 3] + '...'


def parse_variant(code, language):
    """Memoised on (code, language): the twin maker asks for the same text once per weakness class and the tree is never mutated."""
    return _parse_variant(code, language)


@functools.lru_cache(maxsize=16)
def _parse_variant(code, language):
    """(root, lines, text, prefix_length) for the code as written, or wrapped in a class (bare method) or an object literal (property function),
    whichever parses cleanly; None when none does. Byte offsets in the tree minus the prefix length are byte offsets in the original code."""
    kind = LANGUAGES.get(language)
    for grammar in (('ts', 'tsx') if kind == 'ts' else (kind,)):          # JSX inside TypeScript needs the tsx grammar
        parser = _parser(grammar)
        if parser is None:
            return None
        for prefix, body, suffix in (('', code, ''), ('class __W {\n', code, '\n}\n'), ('({\n', code.rstrip().rstrip(','), '\n})\n')):
            text = prefix + body + suffix
            tree = parser.parse(text.encode('utf-8'))
            if not tree.root_node.has_error:
                return tree.root_node, text.split('\n'), text, len(prefix)
    return None


def _parse(code, language):
    parsed = parse_variant(code, language)
    return None if parsed is None else (parsed[0], parsed[1])


def _pattern_names(node):
    """Identifiers bound by a parameter or declaration pattern, skipping type annotations and default values."""
    names = []

    def walk(n):
        if n.type in ('type_annotation', 'type_predicate_annotation'):
            return
        if n.type == 'assignment_pattern':
            walk(n.child_by_field_name('left'))
            return
        if n.type in ('identifier', 'shorthand_property_identifier_pattern'):
            names.append(_text(n))
            return
        if n.type == 'pair_pattern':
            walk(n.child_by_field_name('value'))
            return
        for child in n.named_children:
            walk(child)
    walk(node)
    return names


def _pattern_name_rows(node):
    """(name, zero-based row) of every identifier bound by a parameter or declaration pattern, in source order."""
    found = []

    def walk(n):
        if n.type in ('type_annotation', 'type_predicate_annotation'):
            return
        if n.type == 'assignment_pattern':
            walk(n.child_by_field_name('left'))
            return
        if n.type in ('identifier', 'shorthand_property_identifier_pattern'):
            found.append((_text(n), n.start_point[0]))
            return
        if n.type == 'pair_pattern':
            walk(n.child_by_field_name('value'))
            return
        for child in n.named_children:
            walk(child)
    walk(node)
    return found


MAX_TRAIL = 6


def _is_sanitizer(name, cwe):
    if _NOT_A_SANITIZER.search(name):
        return False
    if cwe == 'CWE-22' and re.search(r'encode|escape|quote', name, re.I):
        return False
    if cwe == 'CWE-601' and re.search(r'encode', name, re.I):
        return False
    return bool(_SANITIZER.search(name))


def _last(callee_text):
    return re.split(r'[.?]+', callee_text)[-1]


# A necessary condition for a flow of a class: some word that the class's sink tests look for occurs in the text. Built from the same name sets call_sink and
# assignment_sink use, so a class is skipped only when it cannot report a flow at all (a speed-up, never a change of result; tests compare the two on real functions).
_SINK_WORD_SETS = {
    'CWE-79': {'html', 'sethtml', 'insertAdjacentHTML', 'write', 'send', 'end', 'innerHTML', 'outerHTML', '__html', 'textContent', 'innerText', 'text'},   # the last three: unsafe-substitution twins start from a text sink
    'CWE-78': _EXEC | _SPAWN, 'CWE-77': _EXEC | _SPAWN, 'CWE-89': _SQL_ANY | _SQL_DB,
    'CWE-918': _SSRF | {'get', 'post', 'put', 'delete', 'patch', 'head', 'Url', 'URL', 'Uri', 'URI', 'Remote'},
    'CWE-22': _PATH_SINKS, 'CWE-601': {'redirect', 'location', 'Location', 'assign', 'replace', 'setHeader', 'header', 'append', 'href'},
    'CWE-94': {'eval', 'Function', 'runInContext', 'runInNewContext', 'runInThisContext'},
    'CWE-1321': _MERGE | {'['}, 'CWE-1333': _REGEX_USE | {'RegExp'},
}
SINK_WORDS = {cwe: re.compile('|'.join(re.escape(word) for word in sorted(words))) for cwe, words in _SINK_WORD_SETS.items()}


def might_have_sink(code, cwe):
    """False only when no sink word of the class occurs in the code, so find_flows could not return a flow."""
    pattern = SINK_WORDS.get(cwe)
    return True if pattern is None else pattern.search(code or '') is not None


class _Run:
    def __init__(self, cwe, lines, line_offset=0):
        self.cwe, self.lines, self.line_offset = cwe, lines, line_offset
        self.trail = {}                       # tainted name -> [(name, zero-based row)] from where the value entered to where the name was bound
        self.html_context = any('<' in line for line in lines)
        self.taint, self.regex, self.flows, self.guarded = {}, {}, [], set()
        self.walked, self.descent_keys, self.safe_objects, self.touched = set(), set(), set(), set()
        self.built = set()                    # names bound to a string the code built (template with a substitution, concatenation, or a ternary of those)

    # ---- taint of an expression: the origin text, or None ------------------------------------------------------------------------------------------
    def origin(self, n):
        if n is None:
            return None
        t = n.type
        if t in ('identifier', 'member_expression', 'subscript_expression') and self.guarded and _text(n) in self.guarded:
            return None
        if t == 'identifier':
            return self.taint.get(_text(n))
        if t in ('member_expression', 'subscript_expression'):
            text = _text(n)
            if text in self.taint:
                return self.taint[text]
            if _SOURCE.match(text):
                return f'`{_short(text)}`'
            base = self.origin(n.child_by_field_name('object'))
            if base:
                return f'`{_short(text)}`' if base.startswith('parameter') else base
            if t == 'subscript_expression':
                return self.origin(n.child_by_field_name('index'))
            return None
        if t == 'binary_expression':
            op = _text(n.child_by_field_name('operator'))
            if op in _COMPARISONS:
                return None
            return _pick([self.origin(n.child_by_field_name('left')), self.origin(n.child_by_field_name('right'))])
        if t == 'template_string':
            return _pick([self.origin(c) for child in n.named_children if child.type == 'template_substitution' for c in child.named_children])
        if t in ('parenthesized_expression', 'await_expression', 'as_expression', 'satisfies_expression', 'non_null_expression', 'type_assertion',
                 'spread_element', 'sequence_expression'):
            return next((self.origin(c) for c in n.named_children if c.type != 'type_annotation' and self.origin(c)), None)
        if t == 'ternary_expression':
            consequence, alternative = n.child_by_field_name('consequence'), n.child_by_field_name('alternative')
            condition = n.child_by_field_name('condition')
            checked = self.checked_expressions(condition) if condition is not None else set()
            negated = condition is not None and _text(condition).lstrip('( ').startswith('!')
            if checked:                                  # `isSafe(x) ? x : fallback`: the branch the check protects carries the checked value, not the caller's
                guarded = alternative if negated else consequence
                if guarded is not None and _text(guarded) in checked:
                    return self.origin(consequence if guarded is alternative else alternative)
            return self.origin(consequence) or self.origin(alternative)
        if t == 'unary_expression':
            return None if _text(n.child_by_field_name('operator')) in ('!', 'typeof', 'void', '-', '+', '~') else self.origin(n.child_by_field_name('argument'))
        if t in ('object', 'array'):
            for child in n.named_children:
                value = child.child_by_field_name('value') if child.type == 'pair' else child
                found = self.origin(value)
                if found:
                    return found
            return None
        if t in ('call_expression', 'new_expression'):
            callee = n.child_by_field_name('function') or n.child_by_field_name('constructor')
            name = _last(_text(callee)) if callee is not None else ''
            if _is_sanitizer(name, self.cwe) or name in _NONSTRING_CALLS:
                return None
            args = n.child_by_field_name('arguments')
            values = list(args.named_children) if args is not None else []
            if name in ('join', 'resolve') and len(values) > 1 and _BASE_DIR.match(_text(values[0])):
                values = values[1:]                      # a conventional base directory is the trusted part of a joined path
            found = _pick([self.origin(arg) for arg in values])
            if found:
                return found
            if callee is not None and callee.type == 'member_expression':
                return self.origin(callee.child_by_field_name('object'))
            return None
        return None

    # ---- statements ---------------------------------------------------------------------------------------------------------------------------
    def chain(self, node):
        """The trail of the first tainted name or tainted member text inside `node` (empty when the value comes straight from a global source)."""
        if node is None:
            return []
        for inner in _descendants(node):
            if inner.type in ('identifier', 'member_expression'):
                key = _text(inner)
                if key in self.trail and key in self.taint:
                    return list(self.trail[key])
        return []

    def extend_trail(self, name, value, row):
        steps = [step for step in self.chain(value) if step[0] != name or step[1] != row] + [(name, row)]
        self.trail[name] = steps if len(steps) <= MAX_TRAIL else steps[:1] + steps[-(MAX_TRAIL - 1):]

    def bind(self, pattern, value):
        names = _pattern_names(pattern) if pattern.type != 'member_expression' else [_text(pattern)]
        origin = self.origin(value)
        row = pattern.start_point[0]
        for name in names:
            if origin:
                self.extend_trail(name, value, row)           # the trail is taken before the name's taint changes
                self.taint[name] = origin
            else:
                self.taint.pop(name, None)
                self.trail.pop(name, None)
        if value is not None and value.type == 'call_expression' and len(names) == 1:
            callee_text = _text(value.child_by_field_name('function'))
            if (callee_text.endswith('Object.create') and _text(value).replace(' ', '').startswith('Object.create(null')) or _NULL_PROTO.search(callee_text):
                self.safe_objects.add(names[0])          # an object without a prototype cannot be used to reach Object.prototype
        inner = value
        while inner is not None and inner.type in ('parenthesized_expression', 'as_expression', 'non_null_expression') and inner.named_children:
            inner = inner.named_children[0]
        if origin and inner is not None and inner.type == 'subscript_expression' and self.origin(inner.child_by_field_name('index')):
            self.walked.update(names)                   # cur = cur[part]: a name that now points into an object chosen by the caller
        if value is not None and value.type == 'regex' and len(names) == 1:
            self.regex[names[0]] = _text(value.child_by_field_name('pattern'))
        if len(names) == 1:
            (self.built.add if self.builds_string(value) else self.built.discard)(names[0])

    def builds_string(self, node):
        """True for a template string with a substitution, a `+` with a string side, or a ternary/parenthesis/await of those; a name bound to one counts."""
        if node is None:
            return False
        t = node.type
        if t == 'identifier':
            return _text(node) in self.built
        if t == 'template_string':
            return any(child.type == 'template_substitution' for child in node.named_children)
        if t == 'binary_expression':
            return _text(node.child_by_field_name('operator')) == '+' and any(
                side is not None and (side.type in ('string', 'template_string') or self.builds_string(side)) for side in (node.child_by_field_name('left'), node.child_by_field_name('right')))
        if t == 'ternary_expression':
            return self.builds_string(node.child_by_field_name('consequence')) or self.builds_string(node.child_by_field_name('alternative'))
        if t in ('parenthesized_expression', 'await_expression', 'as_expression', 'non_null_expression'):
            return any(self.builds_string(child) for child in node.named_children if child.type != 'type_annotation')
        return False

    def visit(self, n):
        t = n.type
        if t in FUNCTIONS:
            params = n.child_by_field_name('parameters') or n.child_by_field_name('parameter')
            for name, row in (_pattern_name_rows(params) if params is not None else []):
                self.taint[name] = f'parameter `{name}`'
                self.trail[name] = [(name, row)]
            if t != 'arrow_function' and 'arguments' not in self.taint:
                self.taint['arguments'] = 'parameter `arguments`'           # the caller's values reach a function that declares no parameter through `arguments`
                self.trail['arguments'] = [('arguments', n.start_point[0])]
        if t == 'if_statement':
            return self.visit_if(n)
        if t == 'variable_declarator':
            value = n.child_by_field_name('value')
            if value is not None:
                self.visit(value)
            self.bind(n.child_by_field_name('name'), value)
            return
        if t == 'assignment_expression':
            left, right = n.child_by_field_name('left'), n.child_by_field_name('right')
            self.visit(right)
            self.assignment_sink(n, left, right)
            if left.type in ('identifier', 'object_pattern', 'array_pattern') or (left.type == 'member_expression' and _text(left).startswith('this.')):
                self.bind(left, right)
            return
        if t == 'augmented_assignment_expression':
            left, right = n.child_by_field_name('left'), n.child_by_field_name('right')
            self.visit(right)
            if self.origin(right) and left.type in ('identifier', 'member_expression'):
                self.extend_trail(_text(left), right, left.start_point[0])
                self.taint[_text(left)] = self.origin(right)
            return
        if t == 'for_in_statement':
            right = n.child_by_field_name('right')
            self.visit(right)
            for name, row in _pattern_name_rows(n.child_by_field_name('left')):
                origin = self.origin(right)
                if origin:
                    self.extend_trail(name, right, row)
                    self.taint[name] = origin
            self.visit(n.child_by_field_name('body'))
            return
        if t == 'expression_statement':
            call = n.named_children[0] if n.named_children else None
            if call is not None and call.type == 'call_expression':
                callee = _last(_text(call.child_by_field_name('function')))
                args = call.child_by_field_name('arguments')
                if _is_sanitizer(callee, self.cwe) and args is not None:
                    for arg in args.named_children:
                        for name in _pattern_names(arg):
                            self.taint.pop(name, None)           # a bare validator call guards its argument for the rest of the function
                        self.touched.add(_text(arg))
        if t == 'call_expression':
            self.call_sink(n)
        if t == 'new_expression':
            self.call_sink(n)
        if t == 'pair' and self.cwe == 'CWE-79':
            key = n.child_by_field_name('key')
            if key is not None and _text(key) == '__html':
                self.report(n, 'dangerouslySetInnerHTML', n.child_by_field_name('value'))
        for child in n.named_children:
            self.visit(child)

    def checked_expressions(self, condition):
        """The exact expressions a condition really checks: arguments and receivers of a validator-named call; for paths, URLs, redirects and prototype
        keys also membership, prefix and pattern calls; and the other side of a comparison with a dangerous literal. A null, type or length test checks
        nothing, a deny-list or suffix test does not make a shell, SQL or HTML string safe, and a guard on `__proto__` alone leaves constructor.prototype.
        A check of one expression says nothing about its neighbours (validating opts.namespace does not protect opts.map)."""
        found, literals = set(), {}
        string_checks = self.cwe in ('CWE-22', 'CWE-601', 'CWE-918', 'CWE-1321')
        for node in _descendants(condition):
            if node.type == 'call_expression':
                callee = node.child_by_field_name('function')
                name = _last(_text(callee))
                if _TYPE_PREDICATE.match(name):
                    continue
                named = _is_sanitizer(name, self.cwe)
                if not (named or (string_checks and name in _CHECK_METHODS)):
                    continue
                args = node.child_by_field_name('arguments')
                found.update(_text(a) for a in (args.named_children if args is not None else []))
                if callee.type == 'member_expression':
                    found.add(_text(callee.child_by_field_name('object')))
            elif node.type == 'binary_expression' and _text(node.child_by_field_name('operator')) in ('==', '===', '!=', '!=='):
                left, right = node.child_by_field_name('left'), node.child_by_field_name('right')
                for literal, other in ((left, right), (right, left)):
                    value = _text(literal).strip('\'"`') if literal.type == 'string' else None
                    if value in _DANGEROUS_LITERALS:
                        literals.setdefault(_text(other), set()).add(value)
        for expression, values in literals.items():
            if self.cwe != 'CWE-1321' or ('__proto__' in values and values & {'constructor', 'prototype'}):
                found.add(expression)
        return found

    def strong_guard(self, condition):
        return bool(self.checked_expressions(condition))

    def note_checks(self, condition):
        """Remember every expression a condition checks in any way (any validator-named or membership/prefix/pattern call, any comparison with a string):
        a flow whose value was touched like that is real but its 'not checked' wording would be false."""
        for node in _descendants(condition):
            if node.type == 'call_expression':
                callee = node.child_by_field_name('function')
                name = _last(_text(callee))
                if not _TYPE_PREDICATE.match(name) and (_is_sanitizer(name, self.cwe) or name in _CHECK_METHODS):
                    args = node.child_by_field_name('arguments')
                    self.touched.update(_text(a) for a in (args.named_children if args is not None else []))
                    if callee.type == 'member_expression':
                        self.touched.add(_text(callee.child_by_field_name('object')))
            elif node.type == 'binary_expression' and _text(node.child_by_field_name('operator')) in ('==', '===', '!=', '!=='):
                for side, other in ((node.child_by_field_name('left'), node.child_by_field_name('right')), (node.child_by_field_name('right'), node.child_by_field_name('left'))):
                    if side.type == 'string':
                        self.touched.add(_text(other))

    def visit_if(self, n):
        condition = n.child_by_field_name('condition')
        self.visit(condition)
        self.note_checks(condition)
        checked = self.checked_expressions(condition)
        saved = {name: self.taint.pop(name) for name in list(self.taint) if name in checked}
        added = checked - self.guarded
        self.guarded |= added
        consequence, alternative = n.child_by_field_name('consequence'), n.child_by_field_name('alternative')
        # Each branch starts from the state before the `if`, and what follows is the join of the ways out: a name that one branch rebinds to a safe value is still
        # tainted on the other path (`loc = url; if (url === 'back') { loc = fallback }; redirect(loc)`).
        start = (dict(self.taint), dict(self.trail))
        after_branches = []
        for branch in (consequence, alternative):
            self.taint.clear(); self.taint.update(start[0]); self.trail.clear(); self.trail.update(start[1])
            if branch is not None:
                self.visit(branch)
            after_branches.append((dict(self.taint), dict(self.trail), branch is not None and any(node.type in ('return_statement', 'throw_statement') for node in _descendants(branch))))
        open_paths = [state for state in after_branches if not state[2]] or after_branches
        self.taint.clear(); self.trail.clear()
        for taint, trail, _exits in open_paths:
            for name, origin in taint.items():
                if name not in self.taint:
                    self.taint[name] = origin
                    if name in trail:
                        self.trail[name] = trail[name]
        exits = any(node.type in ('return_statement', 'throw_statement', 'continue_statement', 'break_statement')
                    for branch in (consequence, alternative) if branch is not None for node in _descendants(branch))
        if not exits:
            for name, origin in saved.items():
                self.taint.setdefault(name, origin)
            self.guarded -= added

    # ---- sinks --------------------------------------------------------------------------------------------------------------------------------
    def report(self, node, sink, argument, anchor=None, also=()):
        origin = self.origin(argument)
        if not origin:
            return
        if anchor is None and node.type == 'call_expression':
            callee = node.child_by_field_name('function')
            anchor = callee.child_by_field_name('property') if callee is not None and callee.type == 'member_expression' else None
        node_row = (anchor or node).start_point[0]
        line = self.lines[node_row].strip()
        arg_text = _short(_text(argument))
        mechanism = MECHANISM.get(self.cwe, '')
        if origin.strip('`') == arg_text.strip('`'):
            root = re.split(r'[.\[]', arg_text)[0]
            origin = f'the caller-supplied `{root}`'                  # never say a value derives from itself
        if sink == 'assignment':
            reason = (f'The write to `{_short(_text(node.child_by_field_name("left")), 40)}` uses `{arg_text}` as the property name, which derives from {origin}; '
                      f'`{arg_text}` is not validated or checked before the write. {mechanism}').strip()
        else:
            reason = (f'`{sink}` receives `{arg_text}`, which derives from {origin}; `{arg_text}` is not escaped, validated or checked before this call. '
                      f'{mechanism}').strip()
        if not any(f['sink'] == sink and f['line'] == line for f in self.flows):
            steps = []
            for part in (argument, *also):
                steps = self.chain(part)
                if steps:
                    break
            identifiers = set()
            inner_check = False
            for part in (argument, *also):
                identifiers |= set(re.findall(r'[A-Za-z_$][\w$]*', _text(part))) | {_text(part)}
                inner_check = inner_check or any(
                    inner.type == 'call_expression' and _is_sanitizer(_last(_text(inner.child_by_field_name('function'))), self.cwe) for inner in _descendants(part))
            self.flows.append({'sink': sink, 'line': line, 'argument': arg_text, 'origin': origin, 'reason': reason, 'row': node_row,
                               'partially_checked': bool(identifiers & self.touched) or inner_check, 'sink_line_no': node_row + 1 - self.line_offset,
                               'steps': [{'name': name, 'line_no': step_row + 1 - self.line_offset} for name, step_row in steps],
                               'assignment': sink == 'assignment', 'target': _short(_text(node.child_by_field_name('left')), 40) if sink == 'assignment' else None})

    def assignment_sink(self, n, left, right):
        text = _text(left)
        if left.type == 'member_expression':
            prop = _text(left.child_by_field_name('property'))
            if self.cwe == 'CWE-79' and prop in ('innerHTML', 'outerHTML'):
                self.report(n, prop, right)
            if self.cwe == 'CWE-601' and (text in ('location', 'window.location', 'document.location') or prop in ('href',) and 'location' in text):
                self.report(n, text, right)
        elif left.type == 'identifier' and self.cwe == 'CWE-601' and text == 'location':
            self.report(n, 'location', right)
        elif left.type == 'subscript_expression' and self.cwe == 'CWE-1321':
            chain, tainted_key, inner_tainted, indexes = left, None, False, []
            while chain is not None and chain.type == 'subscript_expression':
                index = chain.child_by_field_name('index')
                indexes.append(index)
                if self.origin(index):
                    tainted_key = tainted_key or index
                    inner_tainted = inner_tainted or chain is not left
                chain = chain.child_by_field_name('object')
            if chain is not None and chain.type == 'identifier' and _text(chain) in self.safe_objects:
                return
            outer_index = _text(left.child_by_field_name('index'))
            reaches_shared = inner_tainted or (chain is not None and chain.type == 'identifier' and _text(chain) in self.walked) or outer_index in self.descent_keys
            if tainted_key is not None and reaches_shared:
                self.report(n, 'assignment', tainted_key, also=indexes)      # a single-level write only changes that object's own prototype

    def call_sink(self, n):
        callee = n.child_by_field_name('function') or n.child_by_field_name('constructor')
        args = n.child_by_field_name('arguments')
        arg = list(args.named_children) if args is not None else []
        if callee is None:
            return
        full, name = _text(callee), _last(_text(callee))
        receiver = _text(callee.child_by_field_name('object')) if callee.type == 'member_expression' else ''
        cwe = self.cwe
        first = arg[0] if arg else None
        if cwe == 'CWE-79':
            if name in ('html', 'sethtml') and first is not None:
                self.report(n, name, first)
            elif name == 'insertAdjacentHTML' and len(arg) > 1:
                self.report(n, name, arg[1])
            elif full == 'document.write' and first is not None:
                self.report(n, full, first)
            elif name in ('send', 'write', 'end') and receiver and re.match(r'^(?:res|response|reply|ctx\.response|ctx|stream)$', receiver) and first is not None:
                if '<' in _text(first) or self.html_context:
                    self.report(n, full, first)
        elif cwe in ('CWE-77', 'CWE-78'):
            if name in _EXEC and first is not None:
                if not receiver or re.search(r'child_process|childprocess|^cp$|shell|^sh$|execa|\bproc', receiver, re.I) or name != 'exec':
                    self.report(n, name, first)
            elif first is not None and _CMD_WRAPPER.match(name) and self.builds_string(first):
                self.report(n, name, first)
            elif name in _SPAWN and first is not None:
                options = next((a for a in arg if a.type == 'object' and re.search(r'shell\s*:\s*true', _text(a))), None)
                if self.origin(first):
                    self.report(n, name, first)
                elif options is not None:
                    self.report(n, name, next((a for a in arg[1:] if self.origin(a)), first))
        elif cwe == 'CWE-89':
            if first is not None and first.type != 'object' and ((name in _SQL_ANY) or (name in _SQL_DB and _DB_RECEIVER.search(receiver))):
                self.report(n, name, first)
        elif cwe == 'CWE-918':
            if first is not None and ((name in _SSRF and (not receiver or _SSRF_RECEIVER.match(receiver.split('.')[-1]))) or _URL_FETCHER.match(name)
                                      or (name in ('get', 'post', 'put', 'delete', 'patch', 'head') and _SSRF_RECEIVER.match(receiver.split('.')[-1] or 'x'))):
                target = first
                if first.type == 'object':
                    pair = next((c for c in first.named_children if c.type == 'pair' and _text(c.child_by_field_name('key')) in ('url', 'uri', 'href', 'host')), None)
                    target = pair.child_by_field_name('value') if pair is not None else None
                if target is not None:
                    self.report(n, full if receiver else name, target)
        elif cwe == 'CWE-22':
            if name in _PATH_SINKS and (not receiver or _PATH_RECEIVER.match(receiver.split('.')[-1])):
                for target in arg[:2] if name in ('copyFile', 'copyFileSync', 'rename', 'renameSync') else arg[:1]:
                    self.report(n, full, target)
        elif cwe == 'CWE-601':
            if name == 'redirect' and arg:
                self.report(n, full, arg[-1] if len(arg) > 1 and arg[0].type == 'number' else arg[0])
            elif name == 'location' and receiver and first is not None:
                self.report(n, full, first)
            elif name in ('assign', 'replace') and 'location' in receiver and first is not None:
                self.report(n, full, first)
            elif name in ('setHeader', 'set', 'header', 'append') and len(arg) > 1 and _text(arg[0]).strip('\'"`').lower() == 'location':
                self.report(n, full, arg[1])
        elif cwe == 'CWE-94':
            if first is not None and (name == 'eval' or (n.type == 'new_expression' and name == 'Function') or name in ('runInContext', 'runInNewContext', 'runInThisContext')):
                self.report(n, name, first)
        elif cwe == 'CWE-1321':
            if name in _MERGE and len(arg) > 1:
                target = next((a for a in arg[1:] if self.origin(a)), None)
                if target is not None:
                    self.report(n, name, target)
        elif cwe == 'CWE-1333':
            if name == 'RegExp' and first is not None and callee.type == 'identifier':
                self.report(n, 'RegExp', first)                       # a pattern chosen by the caller: its repetition is the attacker's
            elif callee.type == 'member_expression' and name in _REGEX_USE:
                owner = callee.child_by_field_name('object')
                pattern = self.pattern_of(owner)
                if pattern is not None and first is not None and self.origin(first):
                    self.report(n, name, first)
                elif first is not None and self.pattern_of(first) is not None and self.origin(owner):
                    self.report(n, name, owner)

    def pattern_of(self, node):
        """The pattern text of a regex literal (or a variable bound to one) when it has a nested quantifier, else None."""
        if node is None:
            return None
        pattern = None
        if node.type == 'regex':
            pattern = _text(node.child_by_field_name('pattern'))
        elif node.type == 'identifier':
            pattern = self.regex.get(_text(node))
        return pattern if pattern and _NESTED_QUANTIFIER.search(pattern) else None


def _descendants(node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        if current.type not in FUNCTIONS or current is node:
            stack.extend(current.named_children)


def _descent_keys(root):
    """Index texts of subscript arguments passed on to a call (merge(target[key], source[key])): the shape of a recursive merge."""
    keys, stack = set(), [root]
    while stack:
        node = stack.pop()
        if node.type == 'call_expression':
            args = node.child_by_field_name('arguments')
            for arg in (args.named_children if args is not None else []):
                if arg.type == 'subscript_expression':
                    keys.add(_text(arg.child_by_field_name('index')))
        stack.extend(node.named_children)
    return keys


def find_flows(code, language, cwe):
    """Flows of an unsanitised value to a sink of the class, each {'sink', 'line' (verbatim, stripped), 'argument', 'origin', 'reason', 'row'}; [] when the
    code does not parse, the language or class is not supported, or no such flow is visible inside the function."""
    if language == 'python':
        from app.backend.pipeline import grounded_flow_py
        return grounded_flow_py.find_flows(code, cwe) if cwe in SUPPORTED else []
    if cwe not in SUPPORTED or language not in LANGUAGES or not code:
        return []
    parsed = parse_variant(code, language)
    if parsed is None:
        return []
    root, lines, wrapped, prefix = parsed
    run = _Run(cwe, lines, line_offset=wrapped[:prefix].count('\n'))
    run.descent_keys = _descent_keys(root)
    run.visit(root)
    return [flow for flow in run.flows if flow['line'] in code]


def _step_text(step, offset):
    return f"`{step['name']}` (line {step['line_no'] + offset})"


def describe(flow, *, offset=0):
    """(summary, attacker_control, reason) for a confirmed flow, from facts of the shown code only: names, line numbers and the order of the data flow. No
    weakness class, mechanism or impact is named, so the explanation cannot be copied from a class label or a scope line. `offset` is the number of lines shown
    above the code (an import header), so that the numbers are the ones the prompt prints."""
    line = flow['sink_line_no'] + offset
    arg, sink, origin = flow['argument'], flow['sink'], flow['origin']
    steps = flow.get('steps') or []
    if origin.startswith('parameter `') and steps and steps[0]['name'] in origin:
        origin_text = f"parameter `{steps[0]['name']}` (line {steps[0]['line_no'] + offset})"
        later = steps[1:]
    else:
        origin_text, later = origin, steps
    via = (' through ' + ' then '.join(_step_text(step, offset) for step in later)) if later else ''
    if flow.get('assignment'):
        reason = (f"Line {line}: the write to `{flow['target']}` uses `{arg}` as the property name. `{arg}` derives from {origin_text}{via}. "
                  'No validation or check of it appears before the write.')
        summary = f'`{arg}` from {origin_text} is used as a property name unchecked'
    else:
        reason = (f"Line {line}: `{sink}` receives `{arg}`. `{arg}` derives from {origin_text}{via}. "
                  'No escaping, validation or check of it appears before this call.')
        summary = f'`{arg}` from {origin_text} reaches `{sink}` unchecked'
    return summary, origin_text, reason


def confirmed_flow(before, after, language, cwe):
    """The first flow of the vulnerable code that the fixed code no longer has, else None.

    A flow is gone when the fixed code has no flow at the same sink and argument, and also no UNCHECKED flow from the same source to the same sink under a reworded argument:
    rewriting the sink line (a template instead of a concatenation, a renamed temporary) while the value still arrives unchecked is not a fix, and calling it one would be a
    false label."""
    after_flows = find_flows(after, language, cwe)
    after_keys = {(f['sink'], f['argument']) for f in after_flows}
    still_unchecked = {(f['sink'], f['origin']) for f in after_flows if not f['partially_checked']}
    for flow in find_flows(before, language, cwe):
        if (flow['sink'], flow['argument']) in after_keys or flow['partially_checked']:
            continue                     # a flow whose value is checked in some way is real, but 'not checked' would be a false claim
        if (flow['sink'], flow['origin']) in still_unchecked:
            continue
        return flow
    return None
