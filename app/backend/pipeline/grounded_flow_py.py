"""Flow-aware sink detection for Python units (the second language of the grounded corpus).

Same contract as grounded_flow (JavaScript and TypeScript): a value that starts at a parameter or a request-like source is followed through assignments, string building
and calls, stopped at recognised sanitisers and at guards that return or raise, and a sink of the weakness class is reported only when a still-tainted value reaches it.
It looks inside one function, knows no callee bodies and treats a validator-like name as a sanitiser, so a missing flow means "not shown", never "safe".

Each flow has the same fields as the JavaScript analyser's (sink, line, argument, origin, row, partially_checked, sink_line_no, steps, assignment, target), so the
view builder, the explanation (grounded_flow.describe) and the dataset code need no Python-specific branch. The grammar comes from tree-sitter-language-pack.
"""
from __future__ import annotations

import re
import textwrap

_PARSER = None
FUNCTIONS = {'function_definition'}

# The weakness classes the Python sinks cover; CWE-502 (unsafe deserialisation) exists only here.
CLASSES = ('CWE-78', 'CWE-77', 'CWE-94', 'CWE-22', 'CWE-918', 'CWE-89', 'CWE-79', 'CWE-601', 'CWE-502', 'CWE-1333')

_REQUEST = re.compile(r'^(?:flask\.)?request\.(?:args|form|values|json|data|files|cookies|headers|GET|POST|COOKIES|META|body|query_params|path_params|get_json)\b|'
                      r'^(?:sys\.argv|input)\b|^self\.request\.(?:GET|POST|args|body|query_params)\b|^event\b')
_SANITIZER = re.compile(r'escape|sanitiz|quote|clean|valid|verify|assert|ensure|allow|safe|whitelist|basename|normaliz|secure_filename|purif|bleach|^is_(?:valid|safe|allowed|trusted|'
                        r'inside|within|public|local|internal|permitted|secure)|^check_(?:url|path|host|dir|file|name|origin|redirect|safe|allowed|input)', re.I)
_NOT_A_SANITIZER = re.compile(r'unescape|unsanitiz|unsafe|dangerous|raw', re.I)
_NONSTRING_CALLS = {'int', 'float', 'bool', 'len', 'abs', 'round', 'ord', 'hash', 'id', 'isinstance', 'hasattr', 'any', 'all', 'min', 'max', 'sum', 'sorted_len'}
_CHECK_METHODS = {'startswith', 'endswith', 'isalnum', 'isdigit', 'isnumeric', 'isidentifier', 'isalpha', 'match', 'fullmatch', 'is_relative_to', 'relative_to'}
_SHELL_FUNCS = {'os.system', 'os.popen', 'popen', 'commands.getoutput', 'getoutput', 'asyncio.create_subprocess_shell', 'create_subprocess_shell'}
_SUBPROCESS = {'call', 'run', 'Popen', 'check_output', 'check_call', 'getoutput', 'getstatusoutput'}
_FILE_FUNCS = {'open', 'os.remove', 'os.unlink', 'os.rmdir', 'os.makedirs', 'os.mkdir', 'os.listdir', 'os.rename', 'os.chmod', 'os.stat', 'shutil.copy', 'shutil.copyfile',
               'shutil.move', 'shutil.rmtree', 'send_file', 'FileResponse', 'io.open', 'codecs.open'}
_PATH_METHODS = {'read_text', 'read_bytes', 'write_text', 'write_bytes', 'unlink', 'open', 'rmdir', 'mkdir'}
_HTTP_VERBS = {'get', 'post', 'put', 'delete', 'patch', 'head', 'request', 'options'}
_HTTP_RECEIVER = re.compile(r'^(?:requests|httpx|urllib3?|session|client|http|aiohttp|self\.session|self\.client|s)$', re.I)
_URL_FUNCS = {'urlopen', 'urlretrieve', 'urllib.request.urlopen', 'urllib.request.urlretrieve', 'request.urlopen', 'urllib2.urlopen'}
_SQL_METHODS = {'execute', 'executemany', 'executescript', 'raw', 'extra', 'execute_statement', 'execute_sql', 'exec_sql'}
_HTML_FUNCS = {'render_template_string', 'Markup', 'mark_safe', 'HTMLResponse', 'make_response', 'HttpResponse'}
_REDIRECT_FUNCS = {'redirect', 'HttpResponseRedirect', 'RedirectResponse', 'HttpResponsePermanentRedirect'}
_DESERIALIZE = {'pickle.loads', 'pickle.load', 'cPickle.loads', 'cPickle.load', 'marshal.loads', 'marshal.load', 'dill.loads', 'dill.load', 'jsonpickle.decode', 'yaml.unsafe_load',
                'yaml.load', 'yaml.full_load', 'shelve.open'}
_REGEX_FUNCS = {'re.compile', 're.match', 're.search', 're.fullmatch', 're.findall', 're.sub', 're.split', 'compile', 'match', 'search', 'fullmatch'}
_BASE_DIR = re.compile(r'^(?:self\.)?(?:(?:base|root|dir|directory|cwd|prefix|folder|home|upload|storage|media|static|data)\w*|\w*_(?:dir|directory|folder|root))$', re.I)
# Project helpers that run a shell command string; a sink only for a string the code built (f-string, +, %, .format), so a list argument or a quoted value is clean.
_CMD_WRAPPER = re.compile(r'^(?:exec_cmd|run_cmd|run_command|execute_cmd|execute_command|run_shell\w*|shell_exec\w*)$')
_PICKLERS = re.compile(r'pickl|marshal|dill|joblib', re.I)
_UPLOAD_RECEIVER = re.compile(r'file|upload|attach|image|part|document', re.I)
_CONTAINER_ADD = {'append', 'extend', 'add', 'update', 'insert', 'appendleft'}
_JS_EVAL = {'run_javascript', 'execute_script', 'eval_js'}
_HTML_TAG = re.compile(r'<[A-Za-z/!]')
_SUBSCRIPT_TAIL = re.compile(r'[(\[].*$')
_BASE_NAME = re.compile(r'[.\[]')
_SAFE_YAML = re.compile(r'Safe(?:Loader|Dumper)|CSafeLoader|BaseLoader')
SINK_WORDS = re.compile(r'system|popen|subprocess|Popen|\.call|\.run|check_output|eval|exec|open\(|\.remove|unlink|rmtree|send_file|requests|httpx|urlopen|urlretrieve|\.get\(|\.post\('
                        r'|execute|raw\(|render_template_string|Markup|mark_safe|HttpResponse|make_response|redirect|Redirect|pickle|marshal|yaml|dill|jsonpickle|re\.|read_text|read_bytes|'
                        r'\.copy|\.move|FileResponse|exec_cmd|run_cmd|run_command|execute_command|run_shell|shell_exec|loads|joblib|torch|numpy|np\.load|\.save\(|\.append\(|\.write\(|\.extend\(|writelines|'
                        r'run_javascript|execute_script|eval_js|execute_statement|execute_sql|exec_sql|<[A-Za-z/!]')


def _parser():
    global _PARSER
    if _PARSER is None:
        from tree_sitter_language_pack import get_parser
        _PARSER = get_parser('python')
    return _PARSER


def _text(node):
    return node.text.decode('utf-8', 'replace')


def _short(text, limit=60):
    text = ' '.join(text.split())
    return text if len(text) <= limit else text[:limit - 3] + '...'


def _descendants(node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        if current.type not in FUNCTIONS or current is node:
            stack.extend(current.named_children)


def parse(code):
    """(root, lines, dedented code, line offset) or None. A method keeps its indentation in the store, so the code is dedented before parsing."""
    if not code or not code.strip():
        return None
    text = textwrap.dedent(code)
    tree = _parser().parse(text.encode('utf-8'))
    if tree.root_node.has_error:
        return None
    return tree.root_node, text.split('\n'), text, 0


def might_have_sink(code):
    return SINK_WORDS.search(code or '') is not None


def _last(name):
    return re.split(r'[.]+', name)[-1]


def _is_sanitizer(name):
    return not _NOT_A_SANITIZER.search(name) and bool(_SANITIZER.search(name))


def _param_names(node):
    names = []
    for child in node.named_children:
        target = child
        if child.type in ('typed_parameter', 'typed_default_parameter', 'default_parameter'):
            target = next((c for c in child.named_children if c.type == 'identifier'), None)
        elif child.type in ('list_splat_pattern', 'dictionary_splat_pattern'):
            target = next((c for c in child.named_children if c.type == 'identifier'), None)
        if target is not None and target.type == 'identifier':
            names.append((_text(target), target.start_point[0]))
    return [(n, r) for n, r in names if n not in ('self', 'cls')]


class _Run:
    def __init__(self, cwe, lines):
        self.cwe, self.lines = cwe, lines
        self.taint, self.trail, self.flows, self.touched, self.guarded = {}, {}, [], set(), set()
        self.built = set()                    # names bound to a string the code built
        self.js_eval = False                  # True while the argument of a JavaScript-evaluating call is being judged: json.dumps makes a value a safe JS literal there

    # ---- taint of an expression: the origin text, or None --------------------------------------------------------------------------------------
    def origin(self, n):
        if n is None:
            return None
        t = n.type
        if t == 'identifier':
            return None if _text(n) in self.guarded else self.taint.get(_text(n))
        if t in ('attribute', 'subscript'):
            text = _text(n)
            if text in self.guarded:
                return None
            if text in self.taint:
                return self.taint[text]
            if _REQUEST.match(text):
                shown = _SUBSCRIPT_TAIL.sub('', text) if t == 'subscript' else text
                return '`' + _short(shown) + '`'
            base = self.origin(n.child_by_field_name('object') if t == 'attribute' else n.child_by_field_name('value'))
            if base:
                return f'`{_short(text)}`' if base.startswith('parameter') else base
            if t == 'subscript':
                return self.origin(n.child_by_field_name('subscript'))
            return None
        if t == 'binary_operator':
            return self.origin(n.child_by_field_name('left')) or self.origin(n.child_by_field_name('right'))
        if t == 'string':
            parts = [self.origin(c.child_by_field_name('expression')) for c in n.named_children if c.type == 'interpolation']
            return next((p for p in parts if p), None)
        if t == 'concatenated_string':
            return next((self.origin(c) for c in n.named_children if self.origin(c)), None)
        if t in ('parenthesized_expression', 'await', 'list_splat', 'dictionary_splat', 'expression_list'):
            return next((self.origin(c) for c in n.named_children if self.origin(c)), None)
        if t == 'conditional_expression':
            return next((self.origin(c) for c in n.named_children[:1] + n.named_children[2:] if self.origin(c)), None)
        if t in ('list', 'tuple', 'set', 'dictionary', 'pair'):
            return next((self.origin(c) for c in n.named_children if self.origin(c)), None)
        if t in ('not_operator', 'comparison_operator', 'boolean_operator', 'unary_operator'):
            return None if t != 'boolean_operator' else (self.origin(n.child_by_field_name('left')) or self.origin(n.child_by_field_name('right')))
        if t == 'call':
            callee = n.child_by_field_name('function')
            name = _text(callee) if callee is not None else ''
            last = _last(name)
            if _is_sanitizer(last) or name in _NONSTRING_CALLS or last in _NONSTRING_CALLS:
                return None
            if self.js_eval and name in ('json.dumps', 'dumps', 'js_literal', 'to_js'):
                return None                                     # a JSON literal cannot break out of the JavaScript string it is placed in
            args = n.child_by_field_name('arguments')
            values = [c for c in (args.named_children if args is not None else []) if c.type != 'comment']
            if last in ('join', 'joinpath') and len(values) > 1 and _BASE_DIR.match(_text(values[0])):
                values = values[1:]                      # a conventional base directory is the trusted part of a joined path
            found = next((self.origin(a.child_by_field_name('value') if a.type == 'keyword_argument' else a) for a in values
                          if self.origin(a.child_by_field_name('value') if a.type == 'keyword_argument' else a)), None)
            if found:
                return found
            if callee is not None and callee.type == 'attribute':
                return self.origin(callee.child_by_field_name('object'))          # 'tpl'.format(x) handled above via args; x.strip() keeps the taint of x
            return None
        return None

    # ---- statements -----------------------------------------------------------------------------------------------------------------------------
    def chain(self, node):
        if node is None:
            return []
        for inner in _descendants(node):
            if inner.type in ('identifier', 'attribute'):
                key = _text(inner)
                if key in self.trail and key in self.taint:
                    return list(self.trail[key])
        return []

    def bind(self, target, value):
        names = [(_text(c), c.start_point[0]) for c in _descendants(target) if c.type == 'identifier'] if target.type != 'attribute' else [(_text(target), target.start_point[0])]
        origin = self.origin(value)
        for name, row in names:
            if origin:
                steps = [s for s in self.chain(value) if s != (name, row)] + [(name, row)]
                self.trail[name] = steps if len(steps) <= 6 else steps[:1] + steps[-5:]
                self.taint[name] = origin
            else:
                self.taint.pop(name, None)
                self.trail.pop(name, None)
        if len(names) == 1:
            (self.built.add if self.builds_string(value) else self.built.discard)(names[0][0])

    def builds_string(self, n):
        """True for an f-string with an interpolation, a `+`/`%` with a string side, a `.format(...)` call, a string-converter call around one of those, or a name bound to one."""
        if n is None:
            return False
        t = n.type
        if t == 'identifier':
            return _text(n) in self.built
        if t == 'string':
            return any(c.type == 'interpolation' for c in n.named_children)
        if t == 'binary_operator':
            return _text(n.child_by_field_name('operator')) in ('+', '%') and any(
                side is not None and (side.type in ('string', 'concatenated_string') or self.builds_string(side)) for side in (n.child_by_field_name('left'), n.child_by_field_name('right')))
        if t == 'call':
            callee = n.child_by_field_name('function')
            if callee is None:
                return False
            if callee.type == 'attribute' and _last(_text(callee)) == 'format':
                return True
            if _last(_text(callee)) in ('smart_bytes', 'smart_str', 'smart_text', 'str', 'bytes', 'encode', 'force_str', 'force_bytes'):
                args = n.child_by_field_name('arguments')
                return args is not None and any(self.builds_string(a) for a in args.named_children)
            return False
        if t in ('parenthesized_expression', 'concatenated_string', 'conditional_expression'):
            return any(self.builds_string(c) for c in n.named_children)
        return False

    def html_string(self, n):
        """A string (or a `+` of strings) that spells out an HTML tag and interpolates a value."""
        if n is None:
            return False
        if n.type == 'string':
            return any(c.type == 'interpolation' for c in n.named_children) and any(_HTML_TAG.search(_text(c)) for c in n.named_children if c.type == 'string_content')
        if n.type == 'binary_operator' and _text(n.child_by_field_name('operator')) == '+':
            sides = (n.child_by_field_name('left'), n.child_by_field_name('right'))
            return any(self.html_string(s) for s in sides) or (any(s is not None and s.type == 'string' and _HTML_TAG.search(_text(s)) for s in sides) and bool(self.origin(n)))
        return False

    def visit(self, n):
        t = n.type
        if t in FUNCTIONS:
            params = n.child_by_field_name('parameters')
            if params is not None:
                for name, row in _param_names(params):
                    self.taint[name] = f'parameter `{name}`'
                    self.trail[name] = [(name, row)]
            self.visit(n.child_by_field_name('body'))
            return
        if t == 'assignment':
            right = n.child_by_field_name('right')
            if right is not None:
                self.visit(right)
                self.bind(n.child_by_field_name('left'), right)
            return
        if t == 'augmented_assignment':
            right, left = n.child_by_field_name('right'), n.child_by_field_name('left')
            self.visit(right)
            if self.cwe == 'CWE-79' and left is not None and self.html_string(right):
                self.report(n, _text(left) + ' +=', right)
            if self.origin(right) and left is not None:
                name, row = _text(left), left.start_point[0]
                self.trail[name] = [s for s in self.chain(right)] + [(name, row)]
                self.taint[name] = self.origin(right)
            return
        if t == 'for_statement':
            right = n.child_by_field_name('right')
            self.visit(right)
            origin = self.origin(right)
            for c in _descendants(n.child_by_field_name('left')):
                if c.type == 'identifier' and origin:
                    self.taint[_text(c)] = origin
                    self.trail[_text(c)] = self.chain(right) + [(_text(c), c.start_point[0])]
            self.visit(n.child_by_field_name('body'))
            return
        if t == 'with_statement':
            clause = next((c for c in n.named_children if c.type == 'with_clause'), None)
            for item in (clause.named_children if clause is not None else []):
                value = item.named_children[0] if item.named_children else None
                if value is not None and value.type == 'as_pattern':
                    target = next((c for c in value.named_children if c.type == 'as_pattern_target'), None)
                    if target is not None and target.named_children and value.named_children:
                        self.bind(target.named_children[0], value.named_children[0])          # `with buffer(addr) as (view, _)`: the names carry the expression's taint
            for child in n.named_children:
                self.visit(child)
            return
        if t == 'if_statement':
            return self.visit_if(n)
        if t == 'assert_statement':
            for c in n.named_children:
                self.untaint_checked(c)
            return
        if t == 'expression_statement':
            call = n.named_children[0] if n.named_children else None
            if call is not None and call.type == 'call':
                callee = _last(_text(call.child_by_field_name('function')))
                if _is_sanitizer(callee):
                    for arg in (call.child_by_field_name('arguments').named_children if call.child_by_field_name('arguments') is not None else []):
                        self.untaint(arg)
        if t == 'call':
            self.call_sink(n)
            self.container_taint(n)
            if n.parent is not None and n.parent.type in ('block', 'module'):            # a call used as a statement; some grammar versions give it no expression_statement wrapper
                callee = _last(_text(n.child_by_field_name('function')))
                arguments = n.child_by_field_name('arguments')
                if _is_sanitizer(callee) and arguments is not None:
                    for argument in arguments.named_children:
                        self.untaint(argument)
        for child in n.named_children:
            self.visit(child)
        if t == 'return_statement' and self.cwe == 'CWE-79':                              # the markup is built and returned; a call sink on the same line already said it
            value = n.named_children[0] if n.named_children else None
            if (value is not None and value.type in ('string', 'binary_operator', 'concatenated_string', 'call') and '<' in _text(value) and self.origin(value)
                    and not any(f['row'] == n.start_point[0] for f in self.flows)):
                self.report(n, 'return', value)

    def container_taint(self, n):
        """`parts.append(value)` and its kin: a container that was given a tainted value is tainted (a name or an attribute, not a call result)."""
        callee = n.child_by_field_name('function')
        if callee is None or callee.type != 'attribute' or _last(_text(callee)) not in _CONTAINER_ADD:
            return
        holder = callee.child_by_field_name('object')
        if holder is None or holder.type not in ('identifier', 'attribute'):
            return
        args = n.child_by_field_name('arguments')
        origin = next((self.origin(a) for a in (args.named_children if args is not None else []) if a.type != 'comment' and self.origin(a)), None)
        if origin and _text(holder) not in self.guarded:
            name = _text(holder)
            self.trail[name] = [s for s in self.chain(args)] + [(name, holder.start_point[0])]
            self.taint.setdefault(name, origin)

    def untaint(self, node):
        for c in _descendants(node):
            if c.type in ('identifier', 'attribute'):
                self.taint.pop(_text(c), None)
                self.touched.add(_text(c))

    def checked(self, condition):
        """Expressions a condition really checks: arguments and receivers of a validator-named or membership/prefix call, the left side of `in`, the other side of a constant comparison."""
        found = set()
        for node in _descendants(condition):
            if node.type == 'call':
                callee = node.child_by_field_name('function')
                name = _last(_text(callee))
                if _is_sanitizer(name) or name in _CHECK_METHODS:
                    args = node.child_by_field_name('arguments')
                    found.update(_text(a) for a in (args.named_children if args is not None else []))
                    if callee.type == 'attribute':
                        found.add(_text(callee.child_by_field_name('object')))
            elif node.type == 'comparison_operator':
                operands = node.named_children
                texts = [_text(o) for o in operands]
                if any(op in (' in ', ' not in ') or op == 'in' for op in [_text(c) for c in node.children if not c.is_named]) or ' in ' in _text(node) or ' not in ' in _text(node):
                    found.add(texts[0])
                for o in operands:
                    if o.type in ('string', 'integer') and len(operands) == 2:
                        found.update(t for t in texts if t != _text(o))
        return found

    def untaint_checked(self, condition):
        for key in self.checked(condition):
            self.taint.pop(key, None)
            self.touched.add(key)

    def visit_if(self, n):
        condition = n.child_by_field_name('condition')
        self.visit(condition)
        checked = self.checked(condition)
        self.touched.update(checked)
        saved = {name: self.taint.pop(name) for name in list(self.taint) if name in checked}
        added = checked - self.guarded
        self.guarded |= added
        consequence = n.child_by_field_name('consequence')
        alternatives = [c for c in n.named_children if c.type in ('else_clause', 'elif_clause')]
        branches = [consequence, *alternatives]
        for branch in branches:
            if branch is not None:
                self.visit(branch)
        exits = any(d.type in ('return_statement', 'raise_statement', 'continue_statement', 'break_statement') for b in branches if b is not None for d in _descendants(b))
        if not exits:
            for name, origin in saved.items():
                self.taint.setdefault(name, origin)
            self.guarded -= added

    # ---- sinks ----------------------------------------------------------------------------------------------------------------------------------
    def report(self, node, sink, argument):
        origin = self.origin(argument)
        if not origin:
            return
        row = node.start_point[0]
        line = self.lines[row].strip()
        arg_text = _short(_text(argument))
        if origin.strip('`') == arg_text.strip('`'):
            origin = 'the caller-supplied `' + _BASE_NAME.split(arg_text)[0] + '`'
        if any(f['sink'] == sink and f['line'] == line for f in self.flows):
            return
        identifiers = set(re.findall(r'[A-Za-z_][\w]*', _text(argument))) | {_text(argument)}
        inner_check = any(c.type == 'call' and _is_sanitizer(_last(_text(c.child_by_field_name('function')))) for c in _descendants(argument))
        steps = self.chain(argument)
        self.flows.append({'sink': sink, 'line': line, 'argument': arg_text, 'origin': origin, 'row': row,
                           'reason': f'`{sink}` receives `{arg_text}`, which derives from {origin}.', 'partially_checked': bool(identifiers & self.touched) or inner_check,
                           'sink_line_no': row + 1, 'steps': [{'name': name, 'line_no': step_row + 1} for name, step_row in steps], 'assignment': False, 'target': None})

    def call_sink(self, n):
        callee = n.child_by_field_name('function')
        args_node = n.child_by_field_name('arguments')
        if callee is None:
            return
        arg = [c for c in (args_node.named_children if args_node is not None else []) if c.type != 'comment']
        positional = [a for a in arg if a.type != 'keyword_argument']
        keywords = {_text(a.child_by_field_name('name')): a.child_by_field_name('value') for a in arg if a.type == 'keyword_argument'}
        full, name = _text(callee), _last(_text(callee))
        receiver = _text(callee.child_by_field_name('object')) if callee.type == 'attribute' else ''
        first = positional[0] if positional else None
        cwe = self.cwe
        if cwe in ('CWE-78', 'CWE-77'):
            if full in _SHELL_FUNCS and first is not None:
                self.report(n, full, first)
            elif receiver in ('subprocess', 'sp', 'subprocess32') and name in _SUBPROCESS and first is not None:
                shell = keywords.get('shell')
                if shell is not None and _text(shell) == 'True':
                    self.report(n, full, first)
            elif first is not None and _CMD_WRAPPER.match(name) and self.builds_string(first):
                self.report(n, full, first)
        elif cwe == 'CWE-94':
            if full in ('eval', 'exec') and first is not None:
                self.report(n, full, first)
        elif cwe == 'CWE-22':
            if full in _FILE_FUNCS and first is not None:
                self.report(n, full, first)
            elif name == 'send_from_directory' and len(positional) > 1:
                self.report(n, full, positional[1])
            elif callee.type == 'attribute' and name in _PATH_METHODS and self.origin(callee.child_by_field_name('object')):
                self.report(n, full, callee.child_by_field_name('object'))
            elif callee.type == 'attribute' and name == 'save' and first is not None and _UPLOAD_RECEIVER.search(receiver) and self.origin(callee.child_by_field_name('object')):
                self.report(n, full, first)          # an uploaded file saved to a path built from its own name
        elif cwe == 'CWE-918':
            target = first if first is not None else keywords.get('url')
            if full in _URL_FUNCS and target is not None:
                self.report(n, full, target)
            elif name in _HTTP_VERBS and _HTTP_RECEIVER.match(receiver.split('.')[-1] or 'x') and target is not None:
                self.report(n, full, positional[1] if name == 'request' and len(positional) > 1 else target)
        elif cwe == 'CWE-89':
            sql_arg = first if first is not None else next((keywords[k] for k in ('statement', 'sql', 'query') if keywords.get(k) is not None), None)
            if callee.type == 'attribute' and name in _SQL_METHODS and sql_arg is not None:
                self.report(n, full, sql_arg)
            elif name == 'text' and first is not None and not receiver:
                self.report(n, full, first)
            elif not receiver and name in _SQL_METHODS and sql_arg is not None:
                self.report(n, full, sql_arg)
        elif cwe == 'CWE-79':
            if name in _HTML_FUNCS and first is not None:
                if name in ('HttpResponse', 'make_response') and '<' not in _text(first):
                    return
                self.report(n, full, first)
            elif callee.type == 'attribute' and name in ('append', 'write', 'extend', 'writelines', 'insert') and positional and self.html_string(positional[-1]):
                self.report(n, full, positional[-1])          # markup built from a value and added to the page being assembled
            elif name in _JS_EVAL and first is not None and self.builds_string(first):
                self.js_eval = True
                try:
                    self.report(n, full, first)
                finally:
                    self.js_eval = False
        elif cwe == 'CWE-601':
            if name in _REDIRECT_FUNCS and first is not None:
                self.report(n, full, first)
        elif cwe == 'CWE-502':
            if full in _DESERIALIZE and first is not None:
                if full in ('yaml.load', 'yaml.full_load') and _SAFE_YAML.search(' '.join(_text(a) for a in arg)):
                    return
                self.report(n, full, first)
            elif first is not None and callee.type == 'attribute' and name in ('loads', 'load') and _PICKLERS.search(receiver):
                self.report(n, full, first)          # ForkingPickler.loads, cloudpickle.loads, joblib.load ...
            elif first is not None and full in ('torch.load',) and not (keywords.get('weights_only') is not None and _text(keywords['weights_only']) == 'True'):
                self.report(n, full, first)
            elif first is not None and full in ('np.load', 'numpy.load') and keywords.get('allow_pickle') is not None and _text(keywords['allow_pickle']) == 'True':
                self.report(n, full, first)
        elif cwe == 'CWE-1333':
            if full in _REGEX_FUNCS and first is not None and (full.startswith('re.') or not receiver):
                self.report(n, full, first)


def find_flows(code, cwe):
    """Flows of an unsanitised value to a sink of the class, each with the fields of grounded_flow.find_flows; [] when the code does not parse or has no such flow."""
    if cwe not in CLASSES or not might_have_sink(code):
        return []
    parsed = parse(code)
    if parsed is None:
        return []
    root, lines, text, _ = parsed
    run = _Run(cwe, lines)
    run.visit(root)
    return [flow for flow in run.flows if flow['line'] in text]
