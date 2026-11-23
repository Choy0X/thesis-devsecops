"""Grounded security views from mined units (security-grounded-corpus/1).

Every answer is assembled from text that mining captured: the advisory summary and details, the fix commit message, the code the
upstream fix removed, and the tests the fix commit added. Nothing is model-written. The prompt contains code and a scope line only: no advisory,
no CVE or GHSA id, no commit message, and the open-review scope does not name the weakness class.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re

from app.backend.pipeline import grounded_flow
from training.finetune import repair_validation
from training.finetune.grounded_trial import strip_line_numbers      # noqa: F401  (the runner and the builder share one definition)
from training.finetune import security_serialization as serial

POLICY = 'security-grounded-views/2'
SCOPED_NEGATIVE = ('No finding is limited to the addressed issue within the supplied review scope; '
                   'other vulnerabilities have not been ruled out.')
OPEN_SCOPE = 'Review the shown code for security vulnerabilities of any class.'
MAX_CODE_CHARS = 4000

# Public MITRE CWE names (static catalogue, provenance "cwe_catalog"); unknown ids fall back to the bare id.
CWE_TITLES = {
    'CWE-20': 'Improper Input Validation',
    'CWE-22': 'Improper Limitation of a Pathname to a Restricted Directory (Path Traversal)',
    'CWE-59': 'Improper Link Resolution Before File Access (Link Following)',
    'CWE-74': 'Improper Neutralization of Special Elements in Output Used by a Downstream Component (Injection)',
    'CWE-77': 'Improper Neutralization of Special Elements used in a Command (Command Injection)',
    'CWE-78': 'Improper Neutralization of Special Elements used in an OS Command (OS Command Injection)',
    'CWE-79': 'Improper Neutralization of Input During Web Page Generation (Cross-site Scripting)',
    'CWE-119': 'Improper Restriction of Operations within the Bounds of a Memory Buffer',
    'CWE-125': 'Out-of-bounds Read',
    'CWE-787': 'Out-of-bounds Write',
    'CWE-190': 'Integer Overflow or Wraparound',
    'CWE-369': 'Divide By Zero',
    'CWE-401': 'Missing Release of Memory after Effective Lifetime (Memory Leak)',
    'CWE-416': 'Use After Free',
    'CWE-457': 'Use of Uninitialized Variable',
    'CWE-476': 'NULL Pointer Dereference',
    'CWE-362': 'Race Condition (Concurrent Execution using Shared Resource with Improper Synchronization)',
    'CWE-248': 'Uncaught Exception (Panic on Unexpected Input)',
    'CWE-617': 'Reachable Assertion',
    'CWE-89': 'Improper Neutralization of Special Elements used in an SQL Command (SQL Injection)',
    'CWE-94': 'Improper Control of Generation of Code (Code Injection)',
    'CWE-113': 'Improper Neutralization of CRLF Sequences in HTTP Headers (HTTP Response Splitting)',
    'CWE-116': 'Improper Encoding or Escaping of Output',
    'CWE-200': 'Exposure of Sensitive Information to an Unauthorized Actor',
    'CWE-287': 'Improper Authentication',
    'CWE-295': 'Improper Certificate Validation',
    'CWE-269': 'Improper Privilege Management',
    'CWE-306': 'Missing Authentication for Critical Function',
    'CWE-327': 'Use of a Broken or Risky Cryptographic Algorithm',
    'CWE-330': 'Use of Insufficiently Random Values',
    'CWE-345': 'Insufficient Verification of Data Authenticity',
    'CWE-346': 'Origin Validation Error',
    'CWE-352': 'Cross-Site Request Forgery (CSRF)',
    'CWE-400': 'Uncontrolled Resource Consumption',
    'CWE-444': 'Inconsistent Interpretation of HTTP Requests (HTTP Request/Response Smuggling)',
    'CWE-502': 'Deserialization of Untrusted Data',
    'CWE-522': 'Insufficiently Protected Credentials',
    'CWE-532': 'Insertion of Sensitive Information into Log File',
    'CWE-601': 'URL Redirection to Untrusted Site (Open Redirect)',
    'CWE-611': 'Improper Restriction of XML External Entity Reference',
    'CWE-639': 'Authorization Bypass Through User-Controlled Key',
    'CWE-668': 'Exposure of Resource to Wrong Sphere',
    'CWE-693': 'Protection Mechanism Failure',
    'CWE-732': 'Incorrect Permission Assignment for Critical Resource',
    'CWE-754': 'Improper Check for Unusual or Exceptional Conditions',
    'CWE-770': 'Allocation of Resources Without Limits or Throttling',
    'CWE-835': 'Loop with Unreachable Exit Condition (Infinite Loop)',
    'CWE-862': 'Missing Authorization',
    'CWE-863': 'Incorrect Authorization',
    'CWE-915': 'Improperly Controlled Modification of Dynamically-Determined Object Attributes',
    'CWE-918': 'Server-Side Request Forgery (SSRF)',
    'CWE-1284': 'Improper Validation of Specified Quantity in Input',
    'CWE-1321': 'Improperly Controlled Modification of Object Prototype Attributes (Prototype Pollution)',
    'CWE-1333': 'Inefficient Regular Expression Complexity',
    'CWE-1336': 'Improper Neutralization of Special Elements Used in a Template Engine',
}
# Words that make another function of the same file a hard (look-alike) negative for a weakness class.
CWE_CUES = {
    'CWE-79': ('html', 'innerhtml', 'escape', 'sanitiz', 'render', 'attr'),
    'CWE-22': ('path', 'join(', 'resolve(', 'readfile', 'fs.', 'filename'),
    'CWE-77': ('exec', 'spawn', 'command', 'shell'), 'CWE-78': ('exec', 'spawn', 'command', 'shell'),
    'CWE-89': ('query', 'sql', 'select '), 'CWE-918': ('fetch', 'request', 'url', 'http', 'host'),
    'CWE-1321': ('merge', 'assign', 'extend', '__proto__', 'prototype', 'deepcopy', 'clone'),
    'CWE-1333': ('regexp', 'regex', '.match(', '.test(', '.replace('), 'CWE-400': ('regexp', 'regex', 'while', 'loop', 'limit', 'size'),
    'CWE-94': ('eval', 'function(', 'vm.', 'new function'), 'CWE-200': ('error', 'header', 'log', 'token', 'secret'),
    'CWE-601': ('redirect', 'location', 'url'),
}


def cwe_title(cwe):
    return CWE_TITLES.get(cwe, cwe)


def with_final_newline(code):
    """The code with a final newline added when it has none (not util.norm_code: the shown code and its line numbers stay as mined)."""
    return code if code.endswith('\n') else code + '\n'


def _digest(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=True).encode()).hexdigest()[:24]


def number_lines(code):
    """The code with a right-aligned line number in front of every line ("   1| first line"); lines are counted as the patch counts them."""
    return ''.join(f'{index:>4}| {line}\n' for index, line in enumerate(with_final_newline(code).splitlines(), 1))


def _numberable(code):
    """Numbering is exact only when every line break is a plain newline (a stray carriage return or form feed would shift the count)."""
    text = with_final_newline(code)
    return text.splitlines() == text.split('\n')[:-1]


def _context(file, language, code, scope, numbered=False):
    if numbered:
        return f'[target] {file} ({language}, numbered)\n{number_lines(code)}\n[scope]\n{scope}\n'
    return f'[target] {file} ({language})\n{with_final_newline(code)}\n[scope]\n{scope}\n'


def _subject_line(message):
    return (message or '').strip().splitlines()[0].strip() if (message or '').strip() else ''


_SYMBOL = (re.compile(r'\bfunction\s*\*?\s*([A-Za-z_$][\w$]*)'),
           re.compile(r'(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function|\()'),
           re.compile(r'^\s*(?:async\s+|static\s+|public\s+|private\s+)*([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*(?::\s*[\w<>\[\]|. ]+)?\s*\{', re.M),
           re.compile(r'"([A-Za-z_$][\w$]*)"\s*:\s*function'),
           re.compile(r'\b([A-Za-z_$][\w$]*)\s*:\s*(?:async\s*)?function'))
_KEYWORDS = {'if', 'for', 'while', 'switch', 'catch', 'function', 'return', 'constructor'}


def symbol(code):
    for pattern in _SYMBOL:
        for match in pattern.finditer(code):
            if match.group(1) not in _KEYWORDS:
                return match.group(1)
    return None


def _fix_lines(before, after):
    """(removed lines, kept lines) of the upstream change, stripped, from a line diff of the two versions."""
    old, new = before.splitlines(), after.splitlines()
    removed = []
    for tag, i1, i2, _, _ in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag in ('replace', 'delete'):
            removed.extend(line for line in old[i1:i2])
    return removed


_HINT = re.compile(r'ghsa-|cve-\d|inject|traversal|pollution|xss|sanitiz|prevent|attack|vulnerab|malicious|exploit|security|csrf|ssrf|redos|bypass', re.I)
_COMMENT_START = ('//', '/*', '*', '#')
_SIGNATURE = re.compile(r'^(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function\b|class\b|def\b)|'
                        r'^(?:(?:public|private|protected|static|async|get|set|readonly)\s+)*[A-Za-z_$][\w$]*\s*\([^)]*\)\s*(?::\s*[\w<>\[\]|. ,{}]+)?\s*\{$|'
                        r'^(?:const|let|var)\s+[\w$]+\s*=\s*(?:async\s*)?(?:function\b[^{]*|\([^)]*\)\s*=>|[\w$]+\s*=>)\s*\{?$')


def _comment_text(line):
    stripped = line.strip()
    if stripped.startswith(_COMMENT_START):
        return stripped
    match = re.search(r'\s//\s*(.*)$', line)
    return match.group(1) if match else ''


_ADVISORY_ID = re.compile(r'GHSA(?:-[0-9a-z]{4}){3}|GHSA-\w+|CVE-\d{4}-\d+', re.I)


def scrub_ids(text):
    """Remove advisory identifiers (and the brackets they leave empty) from extracted text: a model must not learn to emit ids."""
    text = _ADVISORY_ID.sub('', text or '')
    text = re.sub(r'\(\s*[,;]?\s*\)|\[\s*\]', '', text)
    return re.sub(r'\s{2,}', ' ', text).strip(' ,;:-')


def has_hint_comment(code):
    """True when a comment names the weakness or the fix (GHSA/CVE ids, injection, prevent, bypass ...): the model would learn the hint, not the code."""
    return bool(_ADVISORY_ID.search(code)) or any(_HINT.search(_comment_text(line)) for line in code.splitlines() if _comment_text(line))


def substantial(code):
    """At least four lines of code (not comments) and 200 characters: shorter units are wrappers, getters and stubs."""
    real = [line for line in code.splitlines() if line.strip() and not line.strip().startswith(_COMMENT_START)]
    return len(real) >= 4 and len(code) >= 200


_IMPORT = re.compile(r'^(?:import\b|export\s+\*|export\s+\{[^}]*\}\s+from|(?:const|let|var)\s+[^=]+=\s*require\(|require\(|from\s+\S+\s+import\b|#include\b)')
_CONSTANT = re.compile(r'^(?:export\s+)?(?:const|let|var)\s+[A-Z][A-Z0-9_]*\s*(?::[^=]+)?=')
_OPENER = re.compile(r'^[\)\]\}]*\s*(?:else\s*)?[\(\{\[]?$')


def _usable_quote(line):
    line = line.strip()
    return (len(line) >= 12 and not line.startswith(_COMMENT_START) and not _SIGNATURE.match(line) and not _IMPORT.match(line)
            and not _CONSTANT.match(line) and not _OPENER.match(line) and re.search(r'[A-Za-z]', line) is not None)


# Narrow sink tokens per weakness class: code that contains one is not offered as a clean neighbour, and a quote that contains one is preferred.
CWE_SINKS = {
    'CWE-79': ('innerhtml', 'outerhtml', 'document.write', 'dangerouslysetinnerhtml', 'insertadjacenthtml', '.html(', 'res.send(', 'res.write(', 'res.end(',
               'setattribute(', 'v-html', 'srcdoc', 'marked(', 'sethtml', 'html +='),
    'CWE-22': ('readfile', 'createreadstream', 'createwritestream', 'path.join', 'path.resolve', 'pathjoin', 'sendfile', 'unlink', 'writefile', 'rmsync', 'statsync',
               'existssync', 'readdir', 'readfilesync', 'copyfile'),
    'CWE-77': ('exec(', 'execsync', 'execasync', 'execpromise', 'execcommand', 'execa(', 'spawn(', 'child_process', 'execfile'),
    'CWE-78': ('exec(', 'execsync', 'execasync', 'execpromise', 'execcommand', 'execa(', 'spawn(', 'child_process', 'execfile'),
    'CWE-89': ('.query(', 'execute(', '.raw(', 'select ', 'insert into', 'andwhere(', '.where(', 'json_extract', 'json_value', 'sequelize.literal'),
    'CWE-918': ('fetch(', 'axios', 'http.get', 'https.get', 'http.request', 'https.request', 'request(', 'got(', 'goto(', 'urlopen', 'needle(', 'superagent',
                'net.connect', 'dns.lookup'),
    'CWE-1321': ('object.assign', '__proto__', 'merge(', 'extend(', 'deepcopy', '[key]', '[k]', 'constructor.prototype'),
    'CWE-94': ('eval(', 'new function', 'vm.run', 'runinnewcontext', 'runinthiscontext'),
    'CWE-601': ('redirect(', 'location.href', 'location =', 'location.replace(', 'location.assign('),
    'CWE-1333': ('new regexp', 'matchall('),
    'CWE-400': ('new regexp', 'repeat(', 'alloc(', 'new array('),
    'CWE-502': ('deserialize', 'unserialize', 'yaml.load', 'node-serialize', 'eval('), 'CWE-611': ('parsexml', 'xml2js', 'libxmljs', 'noent', 'dtdload'),
    'CWE-327': ('createhash(', 'createcipher(', 'md5', 'sha1'), 'CWE-330': ('math.random',),
    'CWE-295': ('rejectunauthorized', 'strictssl', 'checkserveridentity'), 'CWE-915': ('object.assign', '__proto__', 'merge(', 'extend(', '[key]', '[k]'),
}


_STRING = re.compile(r"'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"|`(?:\\.|[^`\\])*`")
# A group that repeats inside a repeated group, or an alternation-free wildcard repeated twice: the classic shapes of catastrophic backtracking.
_NESTED_QUANTIFIER = re.compile(r'\((?:\?:)?[^()\n]*[+*][^()\n]*\)[+*{]|\(\.[+*]\\?[^()\n]*\)[+*]')


def _blank_string(match):
    """A string literal becomes "" ; a template literal keeps its ${...} expressions, because those are code that can carry attacker data."""
    text = match.group(0)
    if text.startswith('`') and '${' in text:
        return '"" + ' + ' + '.join(re.findall(r'\$\{([^}]*)\}', text)) + ' + ""'
    return '""'


def _code_only(line):
    """The line without string literals and trailing comments: a sink word inside a message or a comment is not a call."""
    code = _STRING.sub(_blank_string, line)
    return re.sub(r'\s//.*$', '', code)


def _is_header(code):
    stripped = code.strip()
    return stripped.endswith('{') and bool(re.search(r'\bfunction\b|=>|^\s*(?:async\s+)?[\w$.]+\s*\([^)]*\)\s*\{$', stripped))


def sink_in(line, cwe):
    """The first sink token of the weakness class that the line really executes, as written in the line, else None. Words in strings and comments
    and function headers do not count; for regular-expression denial of service a nested quantifier in the pattern is the sink."""
    code = _code_only(line)
    if _is_header(code):
        return None
    lowered = code.lower()
    for sink in CWE_SINKS.get(cwe, ()):
        at = lowered.find(sink)
        if at >= 0:
            if not re.search(r'[A-Za-z_$]', code[at + len(sink):]):
                continue                   # nothing but constants follows: no data can reach this call
            return code[at:at + len(sink)].strip().rstrip('(').lstrip('.') or None
    if cwe == 'CWE-1333':
        match = _NESTED_QUANTIFIER.search(line)
        if match:
            return match.group(0)
    return None


MAX_SINK_DISTANCE = 3


def _fix_positions(before, after):
    """Zero-based line numbers of the vulnerable code that the fix replaced or deleted, plus the lines either side of a pure insertion."""
    old, new = before.splitlines(), after.splitlines()
    positions = set()
    for tag, i1, i2, _, _ in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag in ('replace', 'delete'):
            positions.update(range(i1, i2))
        elif tag == 'insert':
            positions.update({i1 - 1, i1})
    return positions


def _quote(before, after, cwe=None):
    """A verbatim line of the vulnerable code that executes a sink of the weakness class (never a signature, import, constant, bare opener or comment)
    within MAX_SINK_DISTANCE lines of what the fix changed, the nearest first. Without such a line there is no quote: the reason would name a sink
    that has nothing to do with the fix."""
    if not CWE_SINKS.get(cwe):
        return None
    positions = _fix_positions(before, after)
    if not positions:
        return None
    best = None
    for index, line in enumerate(before.splitlines()):
        if not (_usable_quote(line) and sink_in(line, cwe)):
            continue
        distance = min(abs(index - position) for position in positions)
        if distance <= MAX_SINK_DISTANCE and (best is None or (distance, -len(line)) < best[0]):
            best = ((distance, -len(line)), line.strip())
    return best[1] if best else None


def _changed_lines(before, after):
    old_lines, new_lines = set(with_final_newline(before).splitlines()), set(with_final_newline(after).splitlines())
    added = [line for line in with_final_newline(after).splitlines() if line.strip() and line not in old_lines]
    removed = [line for line in with_final_newline(before).splitlines() if line.strip() and line not in new_lines]
    return added, removed


def _trivial_change(before, after):
    """Only imports, comments or nothing at all changed: such a unit shows no weakness."""
    added, removed = _changed_lines(before, after)
    changed = [line.strip() for line in added + removed]
    return not changed or all(line.startswith(_COMMENT_START) or _IMPORT.match(line) for line in changed)


def _meaningful_change(before, after):
    """The fix changed at least one real code line (not only a signature, comment, import, constant or bracket): the quote may be any sink line of the
    function, so the change itself must carry the evidence that this function was fixed."""
    added, removed = _changed_lines(before, after)
    return any(_usable_quote(line) for line in added + removed)


def _substantive_patch(before, after):
    """A repair must add real code: pure deletions, comment-only, import-only and whitespace-only edits teach nothing about a fix."""
    added, _ = _changed_lines(before, after)
    return not _trivial_change(before, after) and any(not line.strip().startswith(_COMMENT_START) for line in added)


def _delta_tokens(before, after):
    """Identifiers present on only one side of the fix: what the fix introduced or dropped."""
    token = re.compile(r'[A-Za-z_$][\w$]{3,}')
    return set(token.findall(before)) ^ set(token.findall(after))


PAYLOADS = {
    'CWE-79': ('<script', '<img', 'onerror', 'javascript:', '&lt;', '<b>', '"><'), 'CWE-22': ('../', '..\\', '%2e', '/etc/'),
    'CWE-1321': ('__proto__', 'constructor', 'prototype'), 'CWE-78': (';', '|', '&&', '$(', '`'), 'CWE-77': (';', '|', '&&', '$(', '`'),
    'CWE-89': ("'--", ' or ', 'union'), 'CWE-918': ('127.0.0.1', 'localhost', '169.254', 'file://', '10.0.', '0.0.0.0'),
    'CWE-94': ('eval', 'constructor', 'process', 'require('), 'CWE-601': ('//evil', 'http://evil', 'https://evil', '//example'),
    'CWE-1333': ('repeat(',), 'CWE-400': ('repeat(', 'maxsize', 'limit'),
    'CWE-352': ('origin', 'referer', 'csrf'), 'CWE-444': ('\\r\\n', 'content-length', 'transfer-encoding'),
}


_HAPPY = re.compile(r"^\s*(?:it|test)\(\s*['\"`]\s*(?:should\s+)?(?:returns?|gets?|creates?|works?|handles? (?:ordinary|normal|valid)|is defined|exists?)\b", re.I)


def _relevant_test(block, delta, cwe):
    """A test is kept only if it asserts something, is not a happy-path check, and touches what the fix changed or carries a payload of the weakness class."""
    if _HAPPY.match(block):
        return False
    if not re.search(r'\b(expect|assert|should|equal|throws?|rejects?|toBe|toEqual|toThrow|ok)\b', block, re.I):
        return False
    if any(token in block for token in delta):
        return True
    text = block.lower()
    return any(marker.lower() in text for marker in PAYLOADS.get(cwe, ()))


_IMPACT = re.compile(r'\b(allow|allows|could|can|may|lead|leads|result|results|enable|enables|cause|causes|exposes?|expose)\b', re.I)
_UNINFORMATIVE = re.compile(r'^\s*(merge\b|release\b|bump\b|chore\b|v?\d+(\.\d+)*\b|version\b|update\b|wip\b)', re.I)


def known_cwe(unit):
    """The first weakness class of the unit that has a catalogue title and a flow detector, else None. Without a detector no reason can be grounded."""
    for cwe in unit.get('cwe') or []:
        if cwe in CWE_TITLES and cwe in grounded_flow.SUPPORTED:
            return cwe
    return None


def informative_subject(message):
    subject = _subject_line(message)
    if len(subject) < 12 or _UNINFORMATIVE.match(subject) or re.search(r'ghsa-|cve-\d', subject, re.I):
        return ''
    return subject


_GENERIC_TITLE_WORDS = {'improper', 'improperly', 'neutralization', 'generation', 'special', 'elements', 'during', 'control', 'controlled', 'restricted',
                        'directory', 'output', 'downstream', 'component', 'limitation', 'pathname', 'incorrect', 'missing', 'exposure', 'insufficient',
                        'resource', 'resources', 'dynamically', 'determined', 'attributes', 'sensitive', 'information', 'unauthorized', 'critical', 'function'}


def _class_words(cwe):
    """Words that tell a sentence is about the weakness class: the class cues plus the distinctive words of its catalogue name (and its acronym)."""
    title = CWE_TITLES.get(cwe, '').lower()
    words = {w[:6] for w in re.findall(r'[a-z]{6,}', title) if w not in _GENERIC_TITLE_WORDS}
    words |= {w for w in re.findall(r'\(([a-z]{3,6})\)', title)}
    return words | set(CWE_CUES.get(cwe, ()))


def _impact(details, summary, *, cwe=None, names=()):
    """One extracted advisory sentence about the consequence. With a class given, a sentence is used only if it is about that class: it names no
    other CWE, no package or repository, and holds a distinctive word of the class. Otherwise the field is simply left out."""
    body = (details or '').strip()
    if body.startswith(summary.strip()):
        body = body[len(summary.strip()):]
    for sentence in re.split(r'(?<=[.!?])\s+|\n+', body):
        sentence = sentence.strip(' #*-\t')
        if sentence.startswith(('[', '!', '>', '|', '<')) or '](' in sentence or sentence.count('`') > 4:
            continue
        if sentence.startswith(('//', '(')) or not sentence.endswith(('.', '!', '?')):
            continue
        if _ADVISORY_ID.search(sentence):
            continue                   # an extracted sentence is never edited; one that names an advisory id is simply not used
        lowered = sentence.lower()
        if any(name and name in lowered for name in names):
            continue                   # it would teach the model to name products it cannot see
        if cwe:
            if {found.upper() for found in re.findall(r'CWE-\d+', sentence, re.I)} - {cwe}:
                continue               # it describes another weakness class than the one labelled
            if not any(word in lowered for word in _class_words(cwe)):
                continue
        if 40 <= len(sentence) <= 220 and _IMPACT.search(sentence):
            return sentence
    return None


def _names(unit):
    """Lower-case package and repository names that must not appear in an answer."""
    raw = [unit.get('package') or '', (unit.get('repo_url') or '').rstrip('/').rsplit('/', 1)[-1]]
    names = set()
    for item in raw:
        for part in {item, item.split('/')[-1]}:
            part = part.strip().lower().lstrip('@')
            if len(part) >= 3:
                names.add(part)
    return tuple(sorted(names))


def _finding_response(unit, quote, symbol_name, reason):
    cwe = known_cwe(unit)
    if not cwe or not quote or not reason:
        return None
    where = symbol_name or unit['file'].rsplit('/', 1)[-1]
    finding = {'summary': f'{cwe_title(cwe)} in {where}', 'location': unit['file'] + (f':{symbol_name}' if symbol_name else ''),
               'violated_invariant': cwe_title(cwe), 'attacker_control': None,
               }                          # the advisory's impact sentence is not used: in the review it was boilerplate or untrue of the shown code
    finding = {k: v for k, v in finding.items() if v is not None}
    return {'status': 'finding', 'findings': [finding], 'evidence': [{'quote': quote, 'reason': reason}],
            'assumptions': [], 'missing_context': [], 'validation_state': 'not_validated'}


def _negative_response(scope_title):
    return {'status': 'no_finding', 'findings': [], 'evidence': [],
            'assumptions': [SCOPED_NEGATIVE + (f' Scope: {scope_title}.' if scope_title else '')],
            'missing_context': [], 'validation_state': 'not_validated'}


def _valid(task, response):
    answer, problems = serial.parse_response(task, json.dumps(response, ensure_ascii=False))
    return not problems


def _view(unit, kind, task, pool, context, response, **extra):
    return {'view_id': 'gv_' + _digest(unit['unit_id'], kind, context), 'unit_id': unit['unit_id'], 'record_id': unit['record_id'],
            'kind': kind, 'task': task, 'pool': pool, 'context_text': context, 'response': response,
            'issue_group_id': unit.get('issue_group_id'), 'cwe': list(unit.get('cwe') or []), 'package': unit.get('package'),
            'language': unit.get('language'), 'file': unit['file'], **extra}


def _patch(file, before, after):
    lines = list(difflib.unified_diff(with_final_newline(before).splitlines(True), with_final_newline(after).splitlines(True),
                                      fromfile=f'a/{file}', tofile=f'b/{file}', n=2))
    return ''.join(lines)


MAX_PATCH_CHANGED_LINES = 12
# Calls that need no definition in the shown code: language and Node builtins a patch may use.
BUILTIN_CALLS = {
    'String', 'Number', 'Boolean', 'parseInt', 'parseFloat', 'isNaN', 'encodeURIComponent', 'decodeURIComponent', 'encodeURI', 'JSON', 'stringify', 'parse',
    'Object', 'keys', 'values', 'entries', 'create', 'freeze', 'defineProperty', 'hasOwn', 'hasOwnProperty', 'assign', 'Array', 'isArray', 'Math', 'min', 'max',
    'floor', 'round', 'path', 'basename', 'resolve', 'join', 'normalize', 'relative', 'isAbsolute', 'dirname', 'extname', 'startsWith', 'endsWith', 'includes',
    'indexOf', 'slice', 'substring', 'trim', 'toString', 'toLowerCase', 'toUpperCase', 'test', 'match', 'replace', 'split', 'push', 'map', 'filter', 'some',
    'every', 'has', 'get', 'set', 'add', 'Set', 'Map', 'URL', 'Error', 'TypeError', 'RangeError', 'Buffer', 'from', 'alloc', 'new', 'Symbol', 'Promise', 'then', 'catch', 'await', 'async', 'if', 'for', 'while', 'return', 'throw', 'typeof', 'function',
    'require', 'status', 'send', 'json', 'end', 'redirect', 'sendStatus', 'setHeader', 'next', 'log', 'warn', 'error', 'console', 'process', 'cwd', 'sep',
    'lstatSync', 'realpathSync', 'statSync', 'existsSync', 'isInteger', 'isSafeInteger', 'is', 'of', 'bind', 'call', 'apply',
}
# Standard properties and methods a repair may use on any object. Any OTHER member (``this.validatePath``, ``dispatch._403``) must occur somewhere in the shown code:
# a review of the v34 repairs found two that called a method the shown code does not have.
STANDARD_MEMBERS = {
    'length', 'prototype', 'constructor', '__proto__', 'message', 'name', 'code', 'pathname', 'href', 'protocol', 'host', 'hostname', 'origin', 'port', 'search',
    'hash', 'searchParams', 'concat', 'reduce', 'forEach', 'find', 'findIndex', 'lastIndexOf', 'charAt', 'padStart', 'trimStart', 'trimEnd', 'raw', 'sort',
    'reverse', 'shift', 'pop', 'unshift', 'splice', 'flat', 'at', 'repeat', 'localeCompare', 'codePointAt', 'valueOf', 'getPrototypeOf', 'setPrototypeOf',
    'getOwnPropertyNames', 'getOwnPropertyDescriptor', 'fromEntries', 'isFinite', 'trunc', 'abs', 'ceil', 'isPrototypeOf', 'propertyIsEnumerable', 'all', 'size',
    'delete', 'clear', 'toFixed', 'substr', 'search', 'matchAll', 'test', 'exec', 'lastIndex', 'source', 'flags', 'toJSON', 'description', 'cause', 'stack',
}
_MEMBER = re.compile(r'\.\s*([A-Za-z_$][\w$]*)')
_ANY_NAME = re.compile(r'[A-Za-z_$][\w$]*')
_CALL_NAME = re.compile(r'([A-Za-z_$][\w$]*)\s*\(')
_REGEX_LITERAL = re.compile(r'(?<![\w)\]$])/(?![/*])(?:\\.|\[(?:\\.|[^\]\\\n])*\]|[^/\\\n\[])+/[gimsuy]*')


def _added_lines(patch):
    return [line[1:] for line in patch.split('\n') if line.startswith('+') and not line.startswith('+++')]


_KEYWORDS = {'return', 'const', 'let', 'var', 'if', 'else', 'throw', 'new', 'typeof', 'null', 'undefined', 'true', 'false', 'this', 'await', 'async', 'of',
             'in', 'function', 'for', 'while', 'try', 'catch', 'finally', 'instanceof', 'void', 'delete', 'switch', 'case', 'break', 'continue', 'default', 'fs',
             'os', 'RegExp'}
_IDENT = re.compile(r'(?<![\w$.])([A-Za-z_$][\w$]*)')
_DECLARED = re.compile(r'(?:const|let|var|function)\s+([A-Za-z_$][\w$]*)|\(([\w$,\s]*)\)\s*=>|([A-Za-z_$][\w$]*)\s*=>')


def _repair_is_grounded(patch, before):
    """A repair may use only names the model can see (used in the shown code, declared by the patch itself) or standard names, and must not invent an
    ad-hoc regular-expression filter: those are the repairs a review found bypassable or referring to helpers the shown unit does not have."""
    added = [_STRING.sub('""', line) for line in _added_lines(patch)]
    declared = set()
    for line in added:
        for match in _DECLARED.finditer(line):
            declared.update(name.strip() for group in match.groups() if group for name in group.split(',') if name.strip())
    blanked = _STRING.sub('""', before)
    visible = set(_IDENT.findall(blanked))
    members_seen = set(_ANY_NAME.findall(blanked))
    for line in added:
        if _REGEX_LITERAL.search(line):
            return False
        for member in _MEMBER.findall(line):
            if member not in BUILTIN_CALLS and member not in STANDARD_MEMBERS and member not in declared and member not in members_seen:
                return False
        for name in _IDENT.findall(line):
            if name not in BUILTIN_CALLS and name not in _KEYWORDS and name not in declared and name not in visible:
                return False
    return True


def _small_single_hunk(patch):
    """One hunk and at most MAX_PATCH_CHANGED_LINES changed lines: the model can learn to place and count it, and the patch stays local."""
    lines = patch.split('\n')
    hunks = sum(1 for line in lines if line.startswith('@@ '))
    changed = sum(1 for line in lines if line.startswith(('+', '-')) and not line.startswith(('+++', '---')))
    return hunks == 1 and changed <= MAX_PATCH_CHANGED_LINES


def _neighbours(unit, limit):
    """Other functions of the same file at the vulnerable revision that the fix did not touch, look-alikes first."""
    text = unit.get('file_before') or ''
    if not text.strip():
        return []
    try:
        from app.backend.pipeline import funcsplit
        parsed = funcsplit.parse_units(unit['language'], unit['file'], text)
    except Exception:                   # ignored: a file the splitter cannot parse offers no neighbour functions
        return []
    removed = {line.strip() for line in _fix_lines(unit['before'], unit['after']) if line.strip()}
    cues = CWE_CUES.get(known_cwe(unit), ())
    if not cues:
        return []                      # without cues for the class a neighbour would be trivial; no neighbour negative is made
    found = []
    for key, (source, _nested) in parsed.items():
        source = source if isinstance(source, str) else str(source)
        if len(source) > 2500 or not substantial(source) or source.strip() == unit['before'].strip() or has_hint_comment(source):
            continue
        lowered = source.lower()
        if len(source) < 300 or any(sink in lowered for sink in CWE_SINKS.get(known_cwe(unit), ())):
            continue                   # too small to inform, or it holds a sink of the scoped class and could itself be vulnerable
        if grounded_flow.find_flows(source, unit['language'], known_cwe(unit)):
            continue                   # a still-tainted value reaches a sink of the class
        if any(line and line in source for line in removed):
            continue
        score = sum(source.lower().count(cue) for cue in cues)
        if score < 1:
            continue
        found.append((-score, _digest(unit['unit_id'], key), source))
    found.sort()
    return [source for _, _, source in found[:limit]]


MAX_HEADER_LINES, MAX_HEADER_CHARS = 14, 700


def _header(unit):
    """The import lines of the file, shown above a repair target so that names the fix uses (an escape helper the file imports) are visible.

    Empty when the unit has none, or when it is longer than a few lines: a long header would take the prompt budget the code needs."""
    text = (unit.get('exec_context') or '').strip('\n')
    if not text or len(text) > MAX_HEADER_CHARS or len(text.splitlines()) > MAX_HEADER_LINES:
        return ''
    if _ADVISORY_ID.search(text):
        return ''
    return text


# Classes a vulnerable function may be shown under as the WRONG scope. The held-out classes (CWE-918, CWE-94) never appear as a training scope,
# and CWE-77 is left out because it shares its sinks with CWE-78.
OTHER_SCOPES = ('CWE-79', 'CWE-78', 'CWE-22', 'CWE-89', 'CWE-601', 'CWE-1321', 'CWE-1333')


def _other_class(unit, before, cwe):
    """A different class for which the analyser finds NO flow in this vulnerable code, so "no finding for that class" is true of what is shown.

    Look-alikes come first: a class whose sink words occur in the code although no tainted value reaches them. The model is taught to check the
    flow of the scoped class instead of naming a class because a related word is present. None when no other class fits."""
    lowered = before.lower()
    candidates = []
    for other in OTHER_SCOPES:
        if other == cwe or (cwe in ('CWE-77', 'CWE-78') and other in ('CWE-77', 'CWE-78')):
            continue
        if grounded_flow.find_flows(before, unit['language'], other):
            continue
        lookalike = any(sink in lowered for sink in CWE_SINKS.get(other, ()))
        candidates.append((0 if lookalike else 1, _digest(unit['unit_id'], other), other))
    candidates.sort()
    return candidates[0][2] if candidates else None


def _calls(block, name):
    return bool(name) and re.search(r'(?<![\w$])' + re.escape(name) + r'(?![\w$])', block) is not None


def _test_blocks(unit, limit=2, max_chars=1400):
    delta, cwe = _delta_tokens(unit['before'], unit['after']), known_cwe(unit)
    name = symbol(unit['before'])
    blocks = []
    for test in unit.get('tests') or []:
        text = test.get('after') or ''
        for match in re.finditer(r'\b(?:it|test)\(\s*([\'"`])(.+?)\1\s*,', text):
            start = match.start()
            depth, i, quote = 0, match.end() - 1, None
            while i < len(text):
                ch = text[i]
                if quote:
                    if ch == '\\':
                        i += 1
                    elif ch == quote:
                        quote = None
                elif ch in '\'"`':
                    quote = ch
                elif ch == '(':
                    depth += 1
                elif ch == ')':
                    depth -= 1
                    if depth < 0:
                        break
                i += 1
            block = text[start:i + 1] if depth < 0 or i >= len(text) else text[start:i + 1]
            if not block.rstrip().endswith((')', ');')) or len(block) > max_chars:
                continue
            if (_ADVISORY_ID.search(block) or not _calls(block, name) or not _relevant_test(block, delta, cwe)
                    or any(block == existing['test_code'].rstrip('\n') for existing in blocks)):
                continue
            blocks.append({'purpose': 'Checks: ' + match.group(2),
                           'test_code': block + ('\n' if not block.endswith('\n') else ''),
                           'expected_observation': 'Intended to pass on the fixed code (not run).',
                           'execution_state': 'not_run'})
            if len(blocks) >= limit:
                return blocks
    return blocks


def build_unit_views(unit, *, neighbours=1, tests=False):
    """All grounded views for one admitted unit, in a fixed order. Views that cannot be grounded are not produced."""
    before, after = unit['before'], unit['after']
    if not before.strip() or not after.strip() or before == after or len(before) > MAX_CODE_CHARS:
        return []
    cwe = known_cwe(unit)
    if not cwe:
        return []                      # no class with a sink table: nothing a reason could name
    title = cwe_title(cwe)
    scoped_scope = f'Review only for: {title}.'
    found = grounded_flow.confirmed_flow(before, after, unit['language'], cwe)
    quote = found['line'] if found and _usable_quote(found['line']) else None
    out = []
    response = _finding_response(unit, quote, symbol(before), found['reason']) if quote else None
    if (response and quote in before and substantial(before) and not has_hint_comment(before) and not _trivial_change(before, after)
            and _meaningful_change(before, after) and not _ADVISORY_ID.search(after)):
        name = symbol(before)
        open_ctx = _context(unit['file'], unit['language'], before, OPEN_SCOPE)
        scoped_ctx = _context(unit['file'], unit['language'], before, scoped_scope)
        if _valid('discovery', response):
            out.append(_view(unit, 'discovery_open', 'discovery', 'supported', open_ctx, response))
            out.append(_view(unit, 'discovery_scoped', 'discovery', 'supported', scoped_ctx, response))
        header = _header(unit)
        shown_before, shown_after = (header + '\n' + before, header + '\n' + after) if header else (before, after)
        patch = _patch(unit['file'], shown_before, shown_after)
        check = repair_validation.assess_patch(patch, {unit['file']: with_final_newline(shown_before)}, allowed_paths={unit['file']})
        if (check['state'] == 'applies_exactly' and _substantive_patch(before, after) and _small_single_hunk(patch) and _numberable(shown_before)
                and _repair_is_grounded(patch, shown_before)):
            repair = dict(response, patch=patch, behavioral_intent='Preserve the unit\'s ordinary behaviour while removing the weakness named in the finding.')
            if _valid('repair', repair):
                out.append(_view(unit, 'repair', 'repair', 'supported',
                                 _context(unit['file'], unit['language'], shown_before, scoped_scope, numbered=True), repair, header_lines=len(header.splitlines()) if header else 0))
                out.append(_view(unit, 'repair_open', 'repair', 'supported',          # the same repair asked without naming the class
                                 _context(unit['file'], unit['language'], shown_before, OPEN_SCOPE, numbered=True), repair, header_lines=len(header.splitlines()) if header else 0))
        blocks = _test_blocks(unit) if tests else []
        if blocks:
            tests = dict(response, tests=blocks)
            if _valid('regression_test_design', tests):
                out.append(_view(unit, 'regression_tests', 'regression_test_design', 'supported', scoped_ctx, tests))
    if not out:
        return []                      # negatives exist only alongside a grounded positive of the same unit
    if scoped_scope:
        if (len(after) <= MAX_CODE_CHARS and substantial(after) and not has_hint_comment(after)
                and not grounded_flow.find_flows(after, unit['language'], cwe)):          # no flow of the class remains in the fixed code
            out.append(_view(unit, 'negative_fixed', 'discovery', 'scoped_negative',
                             _context(unit['file'], unit['language'], after, scoped_scope), _negative_response(title)))
        for index, source in enumerate(_neighbours(unit, neighbours)):
            out.append(_view(unit, 'negative_neighbour', 'discovery', 'scoped_negative',
                             _context(unit['file'], unit['language'], source, scoped_scope), _negative_response(title), neighbour=index))
        other = _other_class(unit, before, cwe)
        if other:
            out.append(_view(unit, 'negative_other_class', 'discovery', 'scoped_negative',
                             _context(unit['file'], unit['language'], before, f'Review only for: {cwe_title(other)}.'), _negative_response(cwe_title(other)),
                             scope_cwe=other))
        if (int(_digest(unit['unit_id'], 'fixed_open'), 16) % 2 == 0          # half of the units: negatives stay near half of all discovery views
                and len(after) <= MAX_CODE_CHARS and substantial(after) and not has_hint_comment(after)
                and not any(grounded_flow.find_flows(after, unit['language'], c) for c in grounded_flow.SUPPORTED)):
            out.append(_view(unit, 'negative_fixed_open', 'discovery', 'scoped_negative',
                             _context(unit['file'], unit['language'], after, OPEN_SCOPE), _negative_response('any class')))
    return [v for v in out if _valid(v['task'], v['response'])]
