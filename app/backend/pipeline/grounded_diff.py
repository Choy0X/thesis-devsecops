"""Diff-grounded evidence for the languages that have no flow analyser (plan v6): PHP, Go, Java, Ruby, C#, Rust, C, C++ and the like.

A unit of a flow-analysed language (Python, JavaScript, TypeScript) is proved by a source-to-sink flow that the fix removes (grounded_flow). The other languages have
no such analyser, so their units are proved by the diff alone, with two weaker kinds of evidence, both tagged on the unit and kept apart in every report:

* `diff_guard`: the fix ADDS a guard (a condition that returns or raises, an assertion, a check call) whose words fit the family of the advisory's class (bounds,
  overflow, null, lifetime, lock, panic and the six families of grounded_guard). The quote is the vulnerable statement the guard now protects.
* `diff_sink`: for the taint classes (XSS, injection, path traversal, redirect, deserialisation, XXE, ReDoS, SSRF, code injection) the fix changes lines within a few
  lines of a sink word of the class in the vulnerable code. The quote is that sink line. This is a necessary condition for a flow, not a proof of one.

Neither proves the weakness; the label is the advisory's class and the reason is a template that names only the line number and generic words (it never mentions the
fixed code, which the model does not see at inference). Python, JavaScript and TypeScript units are never touched by this module (grounded_guard keeps them).
"""
from __future__ import annotations

import difflib
import re

from app.backend.pipeline import grounded_policy
from app.backend.pipeline import grounded_views as g

MAX_CHANGED_LINES = 30
MAX_HUNKS = 3
SINK_DISTANCE = 6
BREADTH_LIMIT = 3          # the weakest evidence is refused for the units of a commit with more units than this (grounded_guard.limit_commit_breadth uses 3 too)
_TEST_PATH = re.compile(r'(^|/)(tests?|__tests__|spec|specs|testing)(/|$)|\.(test|spec)\.[A-Za-z]+$|(^|/)test_[^/]*$|_test\.[A-Za-z]+$')
SCHEME = 'diff_grounded'

# child -> parent class (the v4 aliases of the catalogue are applied first); plan v6 adds the memory-safety, concurrency and panic classes
ALIASES = {'CWE-120': 'CWE-119', 'CWE-121': 'CWE-787', 'CWE-122': 'CWE-787', 'CWE-193': 'CWE-787', 'CWE-191': 'CWE-190', 'CWE-680': 'CWE-190', 'CWE-415': 'CWE-416',
           'CWE-908': 'CWE-457', 'CWE-667': 'CWE-362', 'CWE-366': 'CWE-362', 'CWE-755': 'CWE-754', 'CWE-703': 'CWE-754', 'CWE-770': 'CWE-400', 'CWE-674': 'CWE-400',
           'CWE-835': 'CWE-400', 'CWE-863': 'CWE-862', 'CWE-639': 'CWE-862', 'CWE-285': 'CWE-862', 'CWE-284': 'CWE-862', 'CWE-287': 'CWE-306', 'CWE-77': 'CWE-78'}

# new guard families: name -> vocabulary of the guard line (a weak plausibility filter, not a proof)
NEW_FAMILIES = {
    'bounds': re.compile(r'len|size|sizeof|count|bound|\bmax|\bmin|limit|index|\bidx|capacity|\bcap\b|offset|\bnum|ARRAY_SIZE|avail|remain|[<>]=?', re.I),
    'overflow': re.compile(r'overflow|underflow|_MAX|MAX\b|checked_|saturating|wrapping|check_(?:add|mul|sub)|__builtin_\w*overflow|[!=]= ?0|\bdiv|zero|\bsize|\blen|count', re.I),
    'null': re.compile(r'\bNULL\b|\bnil\b|\bnull\b|\bNone\b|!\s*\w|IS_ERR|[!=]= ?0|is_none|is_some|\bisset\b|\bempty\b|\bunwrap|Option|\?', re.I),
    'lifetime': re.compile(r'free|release|\bput\b|\bget\b|ref|kref|unref|close|drop|destroy|dispose|delete|alloc|NULL|owner|valid|alive|live', re.I),
    'lock': re.compile(r'lock|mutex|spin|rcu|atomic|sync|barrier|race|irq|preempt|rwlock|volatile|once|wait|signal', re.I),
    'panic': re.compile(r'unwrap|expect|panic|assert|unreachable|Result|\bErr\b|error|\?|catch|recover|\btry\b|rescue|Option|\bnil\b|\bNone\b|len|empty|\bget\b|is_', re.I),
}
# class -> family, for the new families
NEW_CLASS_FAMILY = {'CWE-787': 'bounds', 'CWE-125': 'bounds', 'CWE-119': 'bounds', 'CWE-190': 'overflow', 'CWE-369': 'overflow', 'CWE-476': 'null', 'CWE-457': 'null',
                    'CWE-416': 'lifetime', 'CWE-401': 'lifetime', 'CWE-362': 'lock', 'CWE-248': 'panic', 'CWE-617': 'panic', 'CWE-754': 'panic'}
# the tree-sitter nodes of a guard, per language group: (if-like nodes, statements that are a guard by themselves)
_IF_NODES = {'if_statement', 'if_expression', 'if', 'unless', 'if_modifier', 'unless_modifier', 'elsif'}
_STANDALONE = {'throw_statement', 'assert_statement'}
_BODY_FIELDS = ('consequence', 'body')
_EXIT = re.compile(r'\b(?:return|throw|raise|panic|abort|exit|goto|break|continue|BUG|bail|die|fail)\b|\?\s*;?\s*$', re.I)
_CHECK_CALL = re.compile(r'\b(?:\w*(?:check|verify|assert|validate|require|ensure|guard|BUG_ON|WARN_ON|IS_ERR|unlikely|likely|panic|abort)\w*)\s*[!(]', re.I)
_DEBUG_WORD = re.compile(r'debug|verbose|log|trace', re.I)
_KEYWORDS = {'if', 'elif', 'not', 'and', 'or', 'true', 'false', 'none', 'null', 'nil', 'undefined', 'is', 'in', 'then', 'unless'}

# ---- sink words per taint class (a necessary condition for a flow: a word the class's sinks are built from) ---------------------------------------------------
_SQL = r'\b(?:query|exec|execute|raw|rawQuery|prepare|queryRow\w*|QueryContext|ExecContext|executeQuery|executeUpdate|createQuery|createNativeQuery|nativeQuery|FromSql\w*|ExecuteSql\w*|ExecuteReader|SqlCommand|mysqli_query|mysql_query|pg_query|find_by_sql|select_all|where)\s*\('
_CMD = r'\b(?:system|exec|shell_exec|passthru|popen|proc_open|pcntl_exec|ProcessBuilder|Process\.Start|Command::new|Command\.new|exec\.Command\w*|spawn|Open3|IO\.popen|Kernel\.system|Runtime\.getRuntime)\b|`[^`]*#\{'
_PATH = r'\b(?:file_get_contents|fopen|readfile|include|include_once|require|require_once|file_put_contents|unlink|move_uploaded_file|File\.(?:open|read|new|join)|FileInputStream|FileOutputStream|Files\.(?:read\w*|write\w*|newInputStream|copy|delete)|Paths\.get|Path::new|fs::(?:read\w*|write\w*|File)|File::open|os\.(?:Open|ReadFile|Create|Remove|MkdirAll)|ioutil\.\w+|filepath\.Join|http\.ServeFile|File\.ReadAllText|File\.Open\w*|Path\.Combine|ZipEntry|fopen|open|openat|filp_open)\b\s*\(?'
_URL = r'\b(?:curl_exec|curl_init|get_headers|Net::HTTP|URI\.open|HTTParty|Faraday|RestClient|http\.(?:Get|Post|NewRequest\w*|Client)|HttpClient\w*|WebClient|HttpURLConnection|openConnection|openStream|RestTemplate|WebRequest|HttpWebRequest|reqwest|ureq|getForObject|file_get_contents|URL)\b'
_REDIR = r'\b(?:header\s*\(\s*[\'"]Location|redirect\w*|sendRedirect|RedirectResponse|redirect_to|Redirect\w*|http\.Redirect|Redirect::to)\b'
_CODE = r'\b(?:eval|assert|create_function|preg_replace|instance_eval|class_eval|module_eval|send|public_send|constantize|ScriptEngine\w*|GroovyShell|SpelExpressionParser|MVEL|OGNL|CSharpScript|Assembly\.Load|template\.HTML|Class\.forName|loadClass)\b'
_DESER = r'\b(?:unserialize|Marshal\.load|YAML\.load|Psych\.load|ObjectInputStream|readObject|XMLDecoder|XStream|BinaryFormatter|NetDataContractSerializer|DeserializeObject|TypeNameHandling|gob\.NewDecoder|yaml\.Unmarshal|enableDefaultTyping|SnakeYAML|serde_pickle)\b'
_HTML = r'\b(?:echo|print|printf|html_safe|raw|innerHTML|Html\.Raw|HtmlString|MarkupString|setHtml|template\.HTML|Write|WriteString|Fprintf|render_to_string|content_tag|SafeString|th:utext|htmlspecialchars|strip_tags)\b'
_XXE = r'\b(?:simplexml_load\w*|DOMDocument|libxml_disable_entity_loader|XMLReader|DocumentBuilderFactory|SAXParserFactory|XMLInputFactory|XmlReader|XmlDocument|XmlTextReader|Nokogiri|REXML|LIBXML_NOENT)\b'
_REGEX = r'\b(?:preg_match\w*|Regexp\.new|Regex\s*\(|new\s+Regex|Pattern\.compile|regexp\.(?:MustCompile|Compile)|Regex::new)\b'
SINK_PATTERNS = {'CWE-89': _SQL, 'CWE-78': _CMD, 'CWE-77': _CMD, 'CWE-22': _PATH, 'CWE-918': _URL, 'CWE-601': _REDIR, 'CWE-94': _CODE, 'CWE-502': _DESER, 'CWE-79': _HTML,
                 'CWE-611': _XXE, 'CWE-1333': _REGEX}
SINK_RE = {cwe: re.compile(pattern, re.I) for cwe, pattern in SINK_PATTERNS.items()}

# ---- answer templates: only the line number and generic words ------------------------------------------------------------------------------------------
GUARD_TEMPLATES = {
    'bounds': ('The access at line {n} is not checked against the size of the buffer',
               'Line {n} reads or writes memory at a position or length that is not checked against the size of the buffer earlier in this function.'),
    'overflow': ('The arithmetic at line {n} is not checked for overflow or a zero divisor',
                 'Line {n} computes a size, count or quotient from values that are not checked for overflow, wrap-around or a zero divisor earlier in this function.'),
    'null': ('The value used at line {n} is not checked before use',
             'Line {n} uses a pointer, reference or optional value that is not checked for null, an error or an empty value earlier in this function.'),
    'lifetime': ('The object used at line {n} may already be released',
                 'Line {n} uses or releases an object whose lifetime and ownership are not guaranteed by anything earlier in this function.'),
    'lock': ('The shared state used at line {n} is accessed without proper synchronisation',
             'Line {n} accesses shared state without holding a lock or otherwise being synchronised with the other users of it in this function.'),
    'panic': ('The statement at line {n} can fail on unexpected input',
              'Line {n} can panic, abort or raise an unhandled error when its input is unexpected, and nothing earlier in this function handles that case.'),
}
SINK_TEMPLATES = {
    'CWE-79': ('The value written at line {n} reaches the page output without HTML escaping',
               'Line {n} writes a value that may come from outside this function into output that is rendered as HTML, and nothing before it in this function escapes it.'),
    'CWE-78': ('The command at line {n} may include unneutralised outside input',
               'Line {n} runs an operating system command that may include a value from outside this function, and nothing before it neutralises shell metacharacters.'),
    'CWE-89': ('The query at line {n} may include unneutralised outside input',
               'Line {n} sends a query to the database that may include a value from outside this function without parameter binding or escaping.'),
    'CWE-22': ('The path used at line {n} may escape the intended directory',
               'Line {n} uses a path that may contain a value from outside this function without checking that it stays inside the intended directory.'),
    'CWE-601': ('The redirect at line {n} may go to an untrusted address',
                'Line {n} redirects to an address that may come from outside this function without checking it against the allowed destinations.'),
    'CWE-502': ('The data decoded at line {n} may be untrusted',
                'Line {n} deserialises data that may come from outside this function without restricting which types or values it can create.'),
    'CWE-611': ('The XML parsed at line {n} may resolve external entities',
                'Line {n} parses XML from a source outside this function without turning off external entity and DTD processing.'),
    'CWE-1333': ('The pattern used at line {n} may take excessive time on crafted input',
                 'Line {n} matches input against a pattern whose matching time may grow very quickly for crafted input.'),
    'CWE-918': ('The request made at line {n} may go to an untrusted address',
                'Line {n} sends a request to an address that may come from outside this function without checking it against the allowed destinations.'),
    'CWE-94': ('The code evaluated at line {n} may include untrusted input',
               'Line {n} evaluates or compiles code that may include a value from outside this function.'),
}
SINK_ALIAS = {'CWE-77': 'CWE-78'}          # CWE-77 is reported as CWE-78 (the v3 catalogue does the same)


def parent(cwe):
    return ALIASES.get(cwe, cwe)


def family_of(cwes):
    """(family, parent class) for the first class of the advisory that belongs to a guard family of this module or of grounded_guard, else None."""
    from app.backend.pipeline import grounded_guard
    for cwe in cwes or []:
        cls = parent(cwe)
        if cls in NEW_CLASS_FAMILY:
            return NEW_CLASS_FAMILY[cls], cls
        if cls in grounded_guard.CLASS_FAMILY:
            return grounded_guard.CLASS_FAMILY[cls], cls
    return None


def sink_class_of(cwes):
    """The first class of the advisory that has a sink list (CWE-77 counted as CWE-78), else None."""
    for cwe in cwes or []:
        cls = SINK_ALIAS.get(parent(cwe), parent(cwe))
        if cls in SINK_RE:
            return cls
    return None


def vocabulary(family):
    from app.backend.pipeline import grounded_guard
    return NEW_FAMILIES[family] if family in NEW_FAMILIES else grounded_guard.FAMILIES[family][1]


def template(evidence):
    """(summary, reason) templates of a diff_grounded evidence dict."""
    if evidence.get('family') == 'sink':
        return SINK_TEMPLATES[evidence['class']]
    if evidence.get('family') == 'change':
        title = g.CWE_TITLES[evidence['class']]
        return (f'The statement at line {{n}} shows the weakness: {title}', f'Line {{n}} is where this function shows the weakness named in the finding ({title}).')
    if evidence['family'] in GUARD_TEMPLATES:
        return GUARD_TEMPLATES[evidence['family']]
    from app.backend.pipeline import grounded_views_evidence as ev
    return ev.TEMPLATES[evidence['family']]


# ---- parsing ---------------------------------------------------------------------------------------------------------------------------------------------
_WRAPS = {
    'java': (('', ''), ('class __W {\n', '\n}\n')), 'csharp': (('', ''), ('class __W {\n', '\n}\n')), 'cpp': (('', ''), ('class __W {\n', '\n};\n')),
    'php': (('<?php\n', ''), ('<?php\nclass __W {\n', '\n}\n')), 'go': (('package __p\n', ''),), 'rust': (('', ''), ('impl __W {\n', '\n}\n')),
}
_PARSERS = {}


def _parser(language):
    if language not in _PARSERS:
        try:
            import tree_sitter_language_pack as tslp
            _PARSERS[language] = tslp.get_parser(language)
        except Exception:                   # ignored: no grammar for the language is cached as None, and parse() then returns None
            _PARSERS[language] = None
    return _PARSERS[language]


def parse(code, language):
    """(root, prefix_lines) of the best parse of the code as written or wrapped (a bare method needs its class), or None. A tree with errors is accepted when no wrapper
    parses cleanly: C macros and unusual syntax set the error flag, and the guard search skips the damaged subtrees instead of losing the whole unit."""
    parser = _parser(language)
    if parser is None:
        return None
    best = None
    for prefix, suffix in _WRAPS.get(language, (('', ''),)):
        root = parser.parse((prefix + code + suffix).encode('utf-8')).root_node
        rows = prefix.count('\n')
        if not root.has_error:
            return root, rows
        if best is None:
            best = (root, rows)
    return best


def _walk(root):
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))


def _text(node):
    return node.text.decode('utf-8', 'replace')


def _debug_only(condition):
    names = [n for n in re.findall(r'[A-Za-z_]\w*', condition.lower()) if n not in _KEYWORDS]
    return bool(names) and all(_DEBUG_WORD.search(n) for n in names)


def _added_rows(old_lines, new_lines):
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    return matcher.get_opcodes(), {j for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag in ('insert', 'replace') for j in range(j1, j2)}


def added_guards(before, after, language):
    """The guard statements `after` has and `before` lacks: [{'line', 'after_line_no' (1-based), 'kind': condition|raise|early_return|call|assert}]. Empty when no
    grammar exists for the language. Nodes inside a damaged subtree and conditions made only of debug, log or trace names are not guards."""
    parsed = parse(after, language)
    if parsed is None:
        return []
    root, prefix_rows = parsed
    new_lines = g.with_final_newline(after).splitlines()
    _, added = _added_rows(g.with_final_newline(before).splitlines(), new_lines)
    found = {}
    for node in _walk(root):
        row = node.start_point[0] - prefix_rows
        if not node.is_named or row not in added or not 0 <= row < len(new_lines) or row in found or node.has_error:
            continue           # a keyword token (`if`) is not a statement; its parent is
        text = new_lines[row].strip()
        if node.type in _IF_NODES:
            condition = node.child_by_field_name('condition')
            if condition is not None and _debug_only(_text(condition)):
                continue
            body = next((node.child_by_field_name(f) for f in _BODY_FIELDS if node.child_by_field_name(f) is not None), None)
            body_text = _text(body) if body is not None else _text(node)
            kind = 'raise' if re.search(r'\b(?:raise|throw|panic|abort|die|BUG)\b', body_text) else 'early_return' if _EXIT.search(body_text) else 'condition'
        elif node.type in _STANDALONE:
            kind = 'assert' if node.type == 'assert_statement' else 'raise'
        elif node.type == 'macro_invocation' and re.match(r'(?:debug_)?(?:assert|ensure|bail|panic|unreachable)\w*!', _text(node)):
            kind = 'assert'
        elif node.type in ('expression_statement', 'call', 'call_expression') and _CHECK_CALL.search(_text(node)):
            kind = 'call'
        elif node.type == 'raise' or (node.type == 'call' and re.match(r'raise\b', _text(node))):
            kind = 'raise'
        else:
            continue
        found[row] = {'line': text, 'after_line_no': row + 1, 'kind': kind}
    return [found[row] for row in sorted(found)]


# ---- the evidence ----------------------------------------------------------------------------------------------------------------------------------------
def _shape(before, after):
    """(opcodes, changed old rows, added count, removed count) when the code is usable, else None."""
    if not before.strip() or not after.strip() or before == after:
        return None
    if len(before) > g.MAX_CODE_CHARS or len(after) > g.MAX_CODE_CHARS or not g._numberable(before) or not g._numberable(after):
        return None
    if not g.substantial(before) or not g.substantial(after):
        return None
    old_lines, new_lines = g.with_final_newline(before).splitlines(), g.with_final_newline(after).splitlines()
    opcodes, added = _added_rows(old_lines, new_lines)
    changes = [op for op in opcodes if op[0] != 'equal']
    removed = sum(i2 - i1 for tag, i1, i2, _, _ in changes if tag in ('delete', 'replace'))
    if not changes or len(changes) > MAX_HUNKS or len(added) + removed > MAX_CHANGED_LINES:
        return None
    return old_lines, changes, len(added), removed


def guard_grounding(unit, family, cls, shape):
    old_lines, changes, added_count, removed = shape
    if removed > max(1, added_count // 3):
        return None
    vocab = vocabulary(family)
    guards = [x for x in added_guards(unit['before'], unit['after'], unit['language']) if vocab.search(x['line'])]
    if not guards:
        return None
    index = guards[0]['after_line_no'] - 1
    start = next((i1 for tag, i1, i2, j1, j2 in changes if j1 <= index < j2), None)
    if start is None:
        return None
    anchor_index = next((i for i in range(start, len(old_lines)) if old_lines[i].strip()), None)
    if anchor_index is None or not g._usable_quote(old_lines[anchor_index]):
        return None
    return {'family': family, 'class': cls, 'guards': guards, 'anchor': old_lines[anchor_index].strip(), 'anchor_line_no': anchor_index + 1, 'scheme': SCHEME,
            'proof': 'diff_guard'}


def sink_grounding(unit, cls, shape):
    old_lines, changes, _, _ = shape
    pattern = SINK_RE[cls]
    changed = sorted({row for tag, i1, i2, _, _ in changes for row in (range(i1, i2) if tag in ('delete', 'replace') else (i1 - 1, i1))})
    best = None
    for index, line in enumerate(old_lines):
        if not pattern.search(line) or not g._usable_quote(line):
            continue
        distance = min(abs(index - row) for row in changed)
        if distance <= SINK_DISTANCE and (best is None or distance < best[0]):
            best = (distance, index)
    if best is None:
        return None
    index = best[1]
    return {'family': 'sink', 'class': cls, 'anchor': old_lines[index].strip(), 'anchor_line_no': index + 1, 'sink_distance': best[0], 'scheme': SCHEME, 'proof': 'diff_sink'}


def titled_class(cwes):
    """The first class of the advisory (children counted for their parent) that has a catalogue title, else None."""
    for cwe in cwes or []:
        cls = parent(cwe)
        if cls in g.CWE_TITLES:
            return cls
    return None


def change_grounding(unit, cls, shape):
    """The weakest evidence: the advisory names the class, the unit passed strict admission (the commit is tied to the unit by a strong attribution), the fix is small and
    focused, and the quote is the first usable vulnerable line the fix replaces or deletes (for a pure insertion, the statement it is placed before). No guard and no sink
    is claimed; the answer says only that the class named by the advisory shows at this line."""
    old_lines, changes, _, _ = shape
    for tag, i1, i2, _, _ in changes:
        rows = range(i1, i2) if tag in ('delete', 'replace') else (i1, i1 - 1)
        row = next((r for r in rows if 0 <= r < len(old_lines) and g._usable_quote(old_lines[r])), None)
        if row is not None:
            return {'family': 'change', 'class': cls, 'anchor': old_lines[row].strip(), 'anchor_line_no': row + 1, 'scheme': SCHEME, 'proof': 'strict_attribution'}
    return None


def diff_grounding(unit, *, breadth_ok=True):
    """The diff-grounded evidence of a unit of a language without a flow analyser, or None. Guard families first (the fix adds a fitting guard), then the sink family
    for taint classes (the fix changes lines near a sink word of the class). Flow-analysed languages are never handled here (grounded_guard owns them)."""
    language = unit.get('language')
    if not grounded_policy.is_diff_only(language) or _parser(language) is None:
        return None
    shape = _shape(unit.get('before') or '', unit.get('after') or '')
    if shape is None:
        return None
    family = family_of(unit.get('cwe'))
    if family is not None:
        found = guard_grounding(unit, family[0], family[1], shape)
        if found is not None:
            return found
    cls = sink_class_of(unit.get('cwe'))
    found = sink_grounding(unit, cls, shape) if cls is not None else None
    if found is None and breadth_ok:
        cls = titled_class(unit.get('cwe'))
        found = change_grounding(unit, cls, shape) if cls is not None else None
    return found


def attach(units):
    """Attach `diff_grounded`, `origin: recovered_D` and `proof` to every diff-only unit that has evidence and none yet; returns (units, counts per proof)."""
    import collections
    counts = collections.Counter()
    per_commit = collections.Counter(u.get('commit_key') for u in units if grounded_policy.is_diff_only(u.get('language')) and not _TEST_PATH.search((u.get('file') or '').lower()))
    for unit in units:
        if unit.get('diff_grounded') or unit.get('llm_attribution') or not grounded_policy.is_diff_only(unit.get('language')):
            continue
        evidence = diff_grounding(unit, breadth_ok=per_commit[unit.get('commit_key')] <= BREADTH_LIMIT)       # a commit that changes many functions singles out none of them
        if evidence is None:
            counts['no_evidence'] += 1
            continue
        unit['diff_grounded'] = evidence
        unit['origin'] = 'recovered_D'
        unit['proof'] = evidence['proof']
        counts[evidence['proof']] += 1
    return units, dict(counts)
