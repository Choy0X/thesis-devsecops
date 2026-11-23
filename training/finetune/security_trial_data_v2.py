"""Deterministic, authored security supervision, version 2. No model-generated source is executed.

Extends the v1 authored corpus (``security_trial_data``, unchanged) with 42 new families of six views each, in three
partitions with disjoint code forms, wrappers and vocabulary: training (18 families), development (12) and a sealed
blind test (12). The original 108 training views are reused byte for byte and the original 18 held-out views become the
legacy regression partition, explicitly no longer untouched.

This is still an authored template corpus: it measures transfer between code forms and wrappers, not independent
security accuracy. Construction labels are not human review receipts.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import difflib
import hashlib
import itertools
import json
import re
from pathlib import Path

from training.finetune import security_trial_data as v1

SCHEMA = 'security-trial-dataset/2'
TAG = 'security_trial_v2'
SIMILARITY_LIMIT = 0.8
MAX_NEW_TOKENS = {'discovery': 384, 'insufficient_context': 384, 'repair': 640, 'regression_test_design': 768}
COMPLETION_CHARS_PER_TOKEN = 3.3        # calibrated below the minimum (3.58) of the pinned v1 token audit
PROMPT_BASE_TOKENS = 700
CONTEXT_CHARS_PER_TOKEN = 2.5
MAX_TOTAL_TOKENS = 3072                 # 75% of the 4096 context, so long prompts are rejected before training
MECHANISMS = ('shell', 'sql', 'html')
LANGUAGES = ('python', 'javascript')
LIMITS = v1.LIMITS + [
    'Version 2 adds 42 new families. Code forms (the vulnerable sink idiom), wrappers and names are disjoint across training, development and blind; '
    'the mechanisms (shell, SQL, HTML), the adapter contracts, the fixed idioms and the surrounding vocabulary of the task text are shared across partitions.',
    'The generator that produces every partition, including the blind one, ships inside the training bundle; the blind DATA is not in the bundle, the plan or the sampler, '
    'but anyone holding the bundle can regenerate it. The blind commitment fixes its identity, not its secrecy.',
    'The legacy regression partition is the inspected v1 holdout and is no longer untouched.',
    'Reference tests of authored families were executed on the host through a byte-identical allowlist (not the container verifier).',
]
TASK_SIDES = [('discovery', 'vulnerable'), ('discovery', 'fixed'), ('repair', 'vulnerable'),
              ('insufficient_context', 'withheld'), ('regression_test_design', 'vulnerable'),
              ('regression_test_design', 'fixed')]

# Training supplement 1 (security trial v6): 24 more training families, four per mechanism and language, in code shapes that need several edits
# (prelude plus call, a helper function, a multi-line call). Only training is extended; development, legacy and blind are byte-identical.
SUPPLEMENT_TAG = TAG + '_s1'
SUPPLEMENTS = {1: dict(tag='trs1', per_cell=4, forms=(5, 6, 7, 8), wrappers=(0, 1, 2), split='train', val_role='', vocab='train_s1')}
SUPPLEMENT_TAGS = {TAG: 0, SUPPLEMENT_TAG: 1}
SUPPLEMENT_LIMITS = {1: 'Training supplement 1 adds 24 training-only families in four new code shapes per mechanism and language: a prelude statement plus the call, '
                        'a second prelude spelling, a helper function plus the call, and a multi-line call or another spelling of the sink. Most of their fixes touch two '
                        'lines and some hunks have unequal old and new line counts; the rest are single-line edits in new shapes. It reuses the training wrappers and the same '
                        'three mechanisms and adapter contracts, and the development, legacy and blind views are unchanged. The prelude-plus-call shapes are structurally close '
                        'to development form 3, so on development views they no longer measure transfer to an unseen shape; the blind partition is the unseen-shape test. '
                        'It is still an authored template corpus and does not measure independent accuracy.'}

PARTITIONS = {
    'train': dict(tag='trn2', per_cell=3, forms=(0, 1, 2), wrappers=(0, 1, 2), split='train', val_role=''),
    'development': dict(tag='dev2', per_cell=2, forms=(3, 3), wrappers=(3, 4), split='val', val_role='security_trial'),
    'blind_test': dict(tag='blk2', per_cell=2, forms=(4, 4), wrappers=(5, 6), split='blind', val_role='blind_synthetic'),
}
VOCAB = {
    'train': dict(verbs=['run', 'launch', 'fetch', 'render', 'show', 'build'], values=['name', 'text', 'term', 'label', 'entry', 'item'],
                  apis=['launcher', 'gateway', 'backend', 'adapter', 'client', 'service'],
                  shell=['archive', 'inventory', 'report', 'ticket', 'backup', 'export'],
                  sql=[('members', 'name'), ('orders', 'customer'), ('users', 'email'), ('parts', 'label'), ('books', 'title'), ('rooms', 'owner')],
                  html=[('p', 'note'), ('li', 'item'), ('div', 'card'), ('span', 'badge'), ('td', 'cell'), ('h2', 'title')]),
    'train_s1': dict(verbs=['sync', 'record', 'submit', 'export', 'stage', 'publish'],
                     values=['subject', 'caption', 'keyword', 'remark', 'headline', 'reference', 'memo', 'handle'],
                     apis=['relay', 'handler', 'conduit', 'broker', 'courier', 'facade', 'proxy', 'hub'],
                     shell=['catalog', 'journal', 'dossier', 'bulletin', 'registry', 'portfolio', 'compendium', 'almanac'],
                     sql=[('invoices', 'buyer'), ('vehicles', 'plate'), ('courses', 'teacher'), ('projects', 'sponsor'),
                          ('parcels', 'sender'), ('recipes', 'author'), ('devices', 'serial'), ('exhibits', 'curator')],
                     html=[('p', 'excerpt'), ('li', 'bullet'), ('div', 'widget'), ('span', 'marker'),
                           ('td', 'figure'), ('h2', 'heading'), ('section', 'summary'), ('em', 'emphasis')]),
    'development': dict(verbs=['process', 'execute', 'load', 'print'], values=['query', 'phrase', 'raw', 'fragment'],
                        apis=['driver', 'port', 'bridge', 'sink'],
                        shell=['digest', 'manifest', 'ledger', 'summary'],
                        sql=[('tickets', 'reporter'), ('teams', 'lead'), ('assets', 'tag'), ('events', 'venue')],
                        html=[('p', 'comment'), ('li', 'entry'), ('div', 'panel'), ('span', 'chip')]),
    'blind_test': dict(verbs=['dispatch', 'perform', 'resolve', 'emit'], values=['needle', 'payload', 'field', 'token'],
                       apis=['channel', 'endpoint', 'connector', 'target'],
                       shell=['bundle', 'snapshot', 'roster', 'transcript'],
                       sql=[('clients', 'company'), ('shifts', 'worker'), ('sensors', 'site'), ('papers', 'author')],
                       html=[('p', 'message'), ('li', 'row'), ('div', 'tile'), ('span', 'tag')]),
}
ORDINARY = {'shell': ['north-wing', 'report_2024', 'q3-totals', 'ledger'],       # single tokens: a shell would not split them, so the ordinary oracle passes on the vulnerable source
            'sql': ['Orcas', 'Ada Lovelace', 'blue-7', 'north'],
            'html': ['hello', 'plain text', 'Tuesday', 'all good']}
BOUNDARY = {'shell': ['x; echo "done" \\ ok', "it's $(id) `x`", 'a|b\nc', 'name "q" & \\n'],
            'sql': ["O'Reilly", "x' OR '1'='1", "a\\b'; --", "it's\ngone"],
            'html': ['<b>inert</b>', 'Tom & "Jerry" <i>', '<script>x()</script>', '5 < 6 && "ok"']}

# -- code forms: (vulnerable prelude, vulnerable expression, fixed prelude, fixed expression) per mechanism and language ------------------------------
SQLQ = 'SELECT id FROM <<T>> WHERE <<K>> = '
PY_FORMS = {
    'shell': [
        dict(vexpr='<<api>>("<<C>> " + <<v>>, shell=True)', fexpr='<<api>>(["<<C>>", <<v>>], shell=False)'),
        dict(vexpr='<<api>>(f"<<C>> {<<v>>}", shell=True)', fexpr='<<api>>(["<<C>>", <<v>>], shell=False)'),
        dict(vexpr='<<api>>("<<C>> %s" % <<v>>, shell=True)', fexpr='<<api>>(["<<C>>", <<v>>], shell=False)'),
        dict(vpre='built_command = "<<C>> " + <<v>>', vexpr='<<api>>(built_command, shell=True)',
             fpre='built_command = ["<<C>>", <<v>>]', fexpr='<<api>>(built_command, shell=False)'),
        dict(vexpr='<<api>>("<<C>> {}".format(<<v>>), shell=True)', fexpr='<<api>>(["<<C>>", <<v>>], shell=False)')],
    'sql': [
        dict(vexpr='<<api>>.execute("' + SQLQ + "'\" + <<v>> + \"'\")", fexpr='<<api>>.execute("' + SQLQ + '?", (<<v>>,))'),
        dict(vexpr='<<api>>.execute(f"' + SQLQ + "'{<<v>>}'\")", fexpr='<<api>>.execute("' + SQLQ + '?", (<<v>>,))'),
        dict(vexpr='<<api>>.execute("' + SQLQ + "'%s'\" % <<v>>)", fexpr='<<api>>.execute("' + SQLQ + '?", (<<v>>,))'),
        dict(vpre='built_query = "' + SQLQ + "'\" + <<v>> + \"'\"", vexpr='<<api>>.execute(built_query)',
             fpre='built_query = "' + SQLQ + '?"', fexpr='<<api>>.execute(built_query, (<<v>>,))'),
        dict(vexpr='<<api>>.execute("' + SQLQ + "'{}'\".format(<<v>>))", fexpr='<<api>>.execute("' + SQLQ + '?", (<<v>>,))')],
    'html': [
        dict(vexpr='"<<<G>>>" + <<v>> + "</<<G>>>"', fexpr='"<<<G>>>" + html.escape(<<v>>, quote=False) + "</<<G>>>"'),
        dict(vexpr='f"<<<G>>>{<<v>>}</<<G>>>"', fexpr='f"<<<G>>>{html.escape(<<v>>, quote=False)}</<<G>>>"'),
        dict(vexpr='"<<<G>>>%s</<<G>>>" % <<v>>', fexpr='"<<<G>>>%s</<<G>>>" % html.escape(<<v>>, quote=False)'),
        dict(vpre='built_markup = "<<<G>>>" + <<v>> + "</<<G>>>"', vexpr='built_markup',
             fpre='built_markup = "<<<G>>>" + html.escape(<<v>>, quote=False) + "</<<G>>>"', fexpr='built_markup'),
        dict(vexpr='"".join(["<<<G>>>", <<v>>, "</<<G>>>"])', fexpr='"".join(["<<<G>>>", html.escape(<<v>>, quote=False), "</<<G>>>"])')],
}
JS_FORMS = {
    'shell': [
        dict(vexpr='<<api>>.exec("<<C>> " + <<v>>)', fexpr='<<api>>.execFile("<<C>>", [<<v>>])'),
        dict(vexpr='<<api>>.exec(`<<C>> ${<<v>>}`)', fexpr='<<api>>.execFile("<<C>>", [<<v>>])'),
        dict(vexpr='<<api>>.exec(["<<C>>", <<v>>].join(" "))', fexpr='<<api>>.execFile("<<C>>", [<<v>>])'),
        dict(vpre='const built_command = "<<C>> " + <<v>>;', vexpr='<<api>>.exec(built_command)',
             fpre='const arg_list = [<<v>>];', fexpr='<<api>>.execFile("<<C>>", arg_list)'),
        dict(vexpr='<<api>>.exec("<<C>> ".concat(<<v>>))', fexpr='<<api>>.execFile("<<C>>", [<<v>>])')],
    'sql': [
        dict(vexpr='<<api>>.get("' + SQLQ + "'\" + <<v>> + \"'\")", fexpr='<<api>>.get("' + SQLQ + '?", [<<v>>])'),
        dict(vexpr='<<api>>.get(`' + SQLQ + "'${<<v>>}'`)", fexpr='<<api>>.get("' + SQLQ + '?", [<<v>>])'),
        dict(vexpr='<<api>>.get(["' + SQLQ + "'\", <<v>>, \"'\"].join(\"\"))", fexpr='<<api>>.get("' + SQLQ + '?", [<<v>>])'),
        dict(vpre='const built_query = "' + SQLQ + "'\" + <<v>> + \"'\";", vexpr='<<api>>.get(built_query)',
             fpre='const built_query = "' + SQLQ + '?";', fexpr='<<api>>.get(built_query, [<<v>>])'),
        dict(vexpr='<<api>>.get("' + SQLQ + "'\".concat(<<v>>, \"'\"))", fexpr='<<api>>.get("' + SQLQ + '?", [<<v>>])')],
    'html': [
        dict(vexpr='(<<api>>.innerHTML = <<v>>)', fexpr='(<<api>>.textContent = <<v>>)'),
        dict(vexpr='<<api>>.insertAdjacentHTML("beforeend", <<v>>)', fexpr='<<api>>.append(<<v>>)'),
        dict(vexpr='(<<api>>.outerHTML = <<v>>)', fexpr='(<<api>>.textContent = <<v>>)'),
        dict(vpre='const built_markup = <<v>>;', vexpr='(<<api>>.innerHTML = built_markup)',
             fpre='const literal_text = <<v>>;', fexpr='(<<api>>.textContent = literal_text)'),
        dict(vexpr='<<api>>.write(<<v>>)', fexpr='<<api>>.writeText(<<v>>)')],
}
HTML_JS_FACTS = [
    'api is a browser DOM element; innerHTML parses markup and textContent assigns literal text.',
    'api is a browser DOM element; insertAdjacentHTML parses markup and append inserts literal text.',
    'api is a browser DOM element; outerHTML parses markup and textContent assigns literal text.',
    'api is a browser DOM element; innerHTML parses markup and textContent assigns literal text.',
    'api is a document writer; write parses markup and writeText inserts literal text.']
HTML_JS_FAKES = [
    ('set innerHTML(value) { capture.push(["markup", value]); }', 'set textContent(value) { capture.push(["text", value]); }'),
    ('insertAdjacentHTML: (position, value) => capture.push(["markup", value])', 'append: (value) => capture.push(["text", value])'),
    ('set outerHTML(value) { capture.push(["markup", value]); }', 'set textContent(value) { capture.push(["text", value]); }'),
    ('set innerHTML(value) { capture.push(["markup", value]); }', 'set textContent(value) { capture.push(["text", value]); }'),
    ('write: (value) => capture.push(["markup", value])', 'writeText: (value) => capture.push(["text", value])')]

# -- supplement forms 5 to 8 (training only, appended: indices 0 to 4 are untouched) ---------------------------------------------------------------
# Keys beyond the originals: vhelper/fhelper (a function placed before the family function), vquote/fquote (the evidence line when it is not
# the prelude or the call). Form 5 and 6: a prelude statement builds the value and the call uses it, so a complete fix edits both lines.
# Form 7: a helper function builds the value, so the fix edits the helper and the call. Form 8: a multi-line call (or another spelling).
PY_FORMS['shell'] += [
    dict(vpre='command_text = f"<<C>> {<<v>>}"', vexpr='<<api>>(command_text, shell=True)',
         fpre='command_text = ["<<C>>", <<v>>]', fexpr='<<api>>(command_text, shell=False)'),
    dict(vpre='command_text = "<<C>> %s" % <<v>>', vexpr='<<api>>(command_text, shell=True)',
         fpre='command_text = ["<<C>>", <<v>>]', fexpr='<<api>>(command_text, shell=False)'),
    dict(vhelper='def compose_command(<<v>>):\n    return "<<C>> " + <<v>>\n\n\n', vexpr='<<api>>(compose_command(<<v>>), shell=True)',
         fhelper='def compose_command(<<v>>):\n    arguments = ["<<C>>", <<v>>]\n    return arguments\n\n\n', fexpr='<<api>>(compose_command(<<v>>), shell=False)',
         vquote='return "<<C>> " + <<v>>', fquote='arguments = ["<<C>>", <<v>>]'),
    dict(vexpr='<<api>>(\n        "<<C>> " + <<v>>,\n        shell=True\n    )', fexpr='<<api>>(\n        ["<<C>>", <<v>>],\n        shell=False\n    )',
         vquote='"<<C>> " + <<v>>,', fquote='["<<C>>", <<v>>],')]
PY_FORMS['sql'] += [
    dict(vpre='query_text = f"' + SQLQ + "'{<<v>>}'\"", vexpr='<<api>>.execute(query_text)',
         fpre='query_text = "' + SQLQ + '?"', fexpr='<<api>>.execute(query_text, (<<v>>,))'),
    dict(vpre='query_text = "' + SQLQ + "'%s'\" % <<v>>", vexpr='<<api>>.execute(query_text)',
         fpre='query_text = "' + SQLQ + '?"', fexpr='<<api>>.execute(query_text, (<<v>>,))'),
    dict(vhelper='def compose_query(<<v>>):\n    return "' + SQLQ + "'\" + <<v>> + \"'\"\n\n\n", vexpr='<<api>>.execute(compose_query(<<v>>))',
         fhelper='def compose_query(<<v>>):\n    return "' + SQLQ + '?"\n\n\n', fexpr='<<api>>.execute(compose_query(<<v>>), (<<v>>,))',
         vquote='return "' + SQLQ + "'\" + <<v>> + \"'\"", fquote='return "' + SQLQ + '?"'),
    dict(vexpr='<<api>>.execute(\n        "' + SQLQ + "'\" + <<v>> + \"'\"\n    )", fexpr='<<api>>.execute(\n        "' + SQLQ + '?",\n        (<<v>>,)\n    )',
         vquote='"' + SQLQ + "'\" + <<v>> + \"'\"", fquote='"' + SQLQ + '?",')]
PY_FORMS['html'] += [
    dict(vpre='markup_text = f"<<<G>>>{<<v>>}</<<G>>>"', vexpr='markup_text',
         fpre='markup_text = f"<<<G>>>{html.escape(<<v>>, quote=False)}</<<G>>>"', fexpr='markup_text'),
    dict(vpre='markup_text = "<<<G>>>%s</<<G>>>" % <<v>>', vexpr='markup_text',
         fpre='markup_text = "<<<G>>>%s</<<G>>>" % html.escape(<<v>>, quote=False)', fexpr='markup_text'),
    dict(vhelper='def compose_markup(<<v>>):\n    return "<<<G>>>" + <<v>> + "</<<G>>>"\n\n\n', vexpr='compose_markup(<<v>>)',
         fhelper='def compose_markup(<<v>>):\n    safe_value = html.escape(<<v>>, quote=False)\n    return "<<<G>>>" + safe_value + "</<<G>>>"\n\n\n', fexpr='compose_markup(<<v>>)',
         vquote='return "<<<G>>>" + <<v>> + "</<<G>>>"', fquote='safe_value = html.escape(<<v>>, quote=False)'),
    dict(vexpr='"<<<G>>>%(value)s</<<G>>>" % {"value": <<v>>}', fexpr='"<<<G>>>%(value)s</<<G>>>" % {"value": html.escape(<<v>>, quote=False)}')]
JS_FORMS['shell'] += [
    dict(vpre='const command_text = `<<C>> ${<<v>>}`;', vexpr='<<api>>.exec(command_text)',
         fpre='const argument_list = [<<v>>];', fexpr='<<api>>.execFile("<<C>>", argument_list)'),
    dict(vpre='const command_text = ["<<C>>", <<v>>].join(" ");', vexpr='<<api>>.exec(command_text)',
         fpre='const argument_list = [<<v>>];', fexpr='<<api>>.execFile("<<C>>", argument_list)'),
    dict(vhelper='function compose_command(<<v>>) {\n  return "<<C>> " + <<v>>;\n}\n\n', vexpr='<<api>>.exec(compose_command(<<v>>))',
         fhelper='function compose_command(<<v>>) {\n  const arguments_list = [<<v>>];\n  return arguments_list;\n}\n\n',
         fexpr='<<api>>.execFile("<<C>>", compose_command(<<v>>))',
         vquote='return "<<C>> " + <<v>>;', fquote='const arguments_list = [<<v>>];'),
    dict(vexpr='<<api>>.exec(\n    "<<C>> " + <<v>>\n  )', fexpr='<<api>>.execFile(\n    "<<C>>",\n    [<<v>>]\n  )',
         vquote='"<<C>> " + <<v>>', fquote='[<<v>>]')]
JS_FORMS['sql'] += [
    dict(vpre='const query_text = `' + SQLQ + "'${<<v>>}'`;", vexpr='<<api>>.get(query_text)',
         fpre='const query_text = "' + SQLQ + '?";', fexpr='<<api>>.get(query_text, [<<v>>])'),
    dict(vpre='const query_text = ["' + SQLQ + "'\", <<v>>, \"'\"].join(\"\");", vexpr='<<api>>.get(query_text)',
         fpre='const query_text = "' + SQLQ + '?";', fexpr='<<api>>.get(query_text, [<<v>>])'),
    dict(vhelper='function compose_query(<<v>>) {\n  return "' + SQLQ + "'\" + <<v>> + \"'\";\n}\n\n", vexpr='<<api>>.get(compose_query(<<v>>))',
         fhelper='function compose_query(<<v>>) {\n  return "' + SQLQ + '?";\n}\n\n', fexpr='<<api>>.get(compose_query(<<v>>), [<<v>>])',
         vquote='return "' + SQLQ + "'\" + <<v>> + \"'\";", fquote='return "' + SQLQ + '?";'),
    dict(vexpr='<<api>>.get(\n    "' + SQLQ + "'\" + <<v>> + \"'\"\n  )", fexpr='<<api>>.get(\n    "' + SQLQ + '?",\n    [<<v>>]\n  )',
         vquote='"' + SQLQ + "'\" + <<v>> + \"'\"", fquote='"' + SQLQ + '?",')]
JS_FORMS['html'] += [
    dict(vpre='let node = <<api>>;', vexpr='(node.innerHTML = <<v>>)', fexpr='(node.textContent = <<v>>)',
         vquote='node.innerHTML = <<v>>', fquote='node.textContent = <<v>>'),
    dict(vexpr='(<<api>>["innerHTML"] = <<v>>)', fexpr='(<<api>>["textContent"] = <<v>>)'),
    dict(vhelper='function render_into(node, <<v>>) {\n  node.innerHTML = <<v>>;\n}\n\n', vexpr='render_into(<<api>>, <<v>>)',
         fhelper='function render_into(node, <<v>>) {\n  node.textContent = <<v>>;\n}\n\n', fexpr='render_into(<<api>>, <<v>>)',
         vquote='node.innerHTML = <<v>>;', fquote='node.textContent = <<v>>;'),
    dict(vexpr='Object.assign(<<api>>, { innerHTML: <<v>> })', fexpr='Object.assign(<<api>>, { textContent: <<v>> })')]
HTML_JS_FACTS += [
    'api is a browser DOM element; innerHTML parses markup and textContent assigns literal text.',
    'api is a browser DOM element; innerHTML parses markup and textContent assigns literal text.',
    'api is a browser DOM element; innerHTML parses markup and textContent assigns literal text.',
    'api is a browser DOM element; assigning innerHTML parses markup and assigning textContent sets literal text.']
HTML_JS_FAKES += [HTML_JS_FAKES[0]] * 4

# -- wrappers: control and data flow around the sink; each partition owns its wrappers ------------------------------------------------------------
PY_WRAPPERS = [
    '    <<E>>'.replace('<<E>>', 'result = <<E>>\n    return result\n'),
    '    if not isinstance(<<v>>, str):\n        raise TypeError("text required")\n    return <<E>>\n',
    '    outcome = [<<E>> for _ in range(1)]\n    return outcome[0]\n',
    '    while True:\n        return <<E>>\n',
    '    handlers = {"go": lambda: <<E>>}\n    return handlers["go"]()\n',
    '    def finish():\n        return <<E>>\n    return finish()\n',
    '    outcome = None\n    try:\n        outcome = <<E>>\n    finally:\n        pass\n    return outcome\n']
JS_WRAPPERS = [
    '  const result = <<E>>;\n  return result;\n',
    '  if (typeof <<v>> !== "string") throw new TypeError("text required");\n  return <<E>>;\n',
    '  const outcome = [0].map(() => <<E>>);\n  return outcome[0];\n',
    '  while (true) {\n    return <<E>>;\n  }\n',
    '  const handlers = { go: () => <<E>> };\n  return handlers.go();\n',
    '  function finish() {\n    return <<E>>;\n  }\n  return finish();\n',
    '  let outcome = null;\n  try {\n    outcome = <<E>>;\n  } finally {}\n  return outcome;\n']


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'))


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _fill(text, **names):
    for key, value in names.items():
        text = text.replace(f'<<{key}>>', value)
    return text


_KEYWORDS = set('''def return if else elif for while in try except finally raise class lambda import from with not is or and
True False None pass const let var function new throw catch typeof null undefined this await async of isinstance str'''.split())
_TOKEN = re.compile(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|\d+|[A-Za-z_$][\w$]*|\s+|.''', re.S)


def skeleton(source):
    """Identifier- and literal-insensitive token skeleton: names, strings and numbers collapse, structure remains."""
    tokens = []
    for token in _TOKEN.findall(source):
        if token.isspace():
            continue
        if token[0] in '"\'`':
            tokens.append('S')
        elif token[0].isdigit():
            tokens.append('N')
        elif re.match(r'[A-Za-z_$]', token):
            tokens.append(token if token in _KEYWORDS else 'I')
        else:
            tokens.append(token)
    return tokens


def _shingles(tokens, n=3):
    return {tuple(tokens[i:i + n]) for i in range(max(1, len(tokens) - n + 1))}


def structural_sha(source):
    return hashlib.sha256(' '.join(skeleton(source)).encode()).hexdigest()


def _normalized(text):
    return v1._normalized(text)


def _literal(value, style):
    escaped = value.replace('\\', '\\\\').replace('\n', '\\n').replace('\t', '\\t')
    if style == 1:
        return "'" + escaped.replace("'", "\\'") + "'"
    return '"' + escaped.replace('"', '\\"') + '"'


def _names(partition, mechanism, language, ordinal):
    vocab = VOCAB[partition]
    verb, value, api = vocab['verbs'][ordinal % len(vocab['verbs'])], vocab['values'][ordinal], vocab['apis'][ordinal]
    item = vocab[mechanism][ordinal]
    noun = item if mechanism == 'shell' else item[1]
    fn = f'{verb}_{noun}' if language == 'python' else verb + noun.capitalize()
    names = {'fn': fn, 'v': value, 'api': api}
    if mechanism == 'shell':
        names['C'] = item
    elif mechanism == 'sql':
        names['T'], names['K'] = item
    else:
        names['G'] = item[0]
    return names


def _sources(mechanism, language, form, wrapper, names):
    forms = (PY_FORMS if language == 'python' else JS_FORMS)[mechanism]
    spec = forms[form]
    wrappers = PY_WRAPPERS if language == 'python' else JS_WRAPPERS
    fill = {k: names[k] for k in names}
    out, quotes = {}, {}
    for side, pre, expr in (('vulnerable', spec.get('vpre'), spec['vexpr']), ('fixed', spec.get('fpre', spec.get('vpre')), spec['fexpr']),
                            ('withheld', None, '<<api>>.apply(<<v>>)')):
        pre_text, expr_text = (_fill(pre, **fill) if pre else None), _fill(expr, **fill)
        helper = spec.get('vhelper' if side == 'vulnerable' else 'fhelper') if side != 'withheld' else None
        helper_text = _fill(helper, **fill) if helper else ''
        indent = '    ' if language == 'python' else '  '
        head = (indent + pre_text + '\n') if pre_text else ''
        body = _fill(wrappers[wrapper], **{**fill, 'E': expr_text})
        if language == 'python':
            prefix = 'import html\n\n' if mechanism == 'html' else ''
            out[side] = prefix + helper_text + f'def {names["fn"]}({names["v"]}, {names["api"]}):\n' + head + body
        else:
            out[side] = helper_text + f'function {names["fn"]}({names["v"]}, {names["api"]}) {{\n' + head + body + '}\n'
        custom = spec.get('vquote' if side == 'vulnerable' else 'fquote') if side != 'withheld' else None
        quotes[side] = _fill(custom, **fill) if custom else (pre_text if pre_text and side != 'withheld' else expr_text)
    return out, quotes


def _facts(mechanism, language, form, names):
    api = names['api']
    if mechanism == 'shell':
        text = 'api has subprocess.run semantics.' if language == 'python' else 'api.exec and api.execFile have Node child_process semantics.'
    elif mechanism == 'sql':
        text = ('api is an SQLite-style adapter: execute(sql, parameters=()) binds each ? placeholder from the parameters tuple; a lone SQL string is parsed as syntax.'
                if language == 'python' else
                'api is an SQLite-style adapter: get(sql, params = []) binds each ? placeholder from params; a lone SQL string is parsed as syntax.')
    elif language == 'python':
        text = 'The returned string is inserted into an HTML body; the adapter parameter api is unused.'
    else:
        text = HTML_JS_FACTS[form]
    return re.sub(r"\bapi\b", api, text)


def _test_code(mechanism, language, form, names, value, style):
    fn, literal = names['fn'], _literal(value, style)
    if language == 'python':
        if mechanism == 'shell':
            return ('capture = []\ndef fake(*args, **kwargs):\n    capture.append((args, kwargs))\n'
                    f'{fn}({literal}, fake)\n'
                    f'assert capture == [(([{_literal(names["C"], 0)}, {literal}],), {{"shell": False}})]\n')
        if mechanism == 'sql':
            query = _literal(_fill(SQLQ, **names) + '?', 0)
            return ('capture = []\nclass Fake:\n    def execute(self, sql, params=()):\n        capture.append((sql, params))\n'
                    f'{fn}({literal}, Fake())\n'
                    f'assert capture == [({query}, ({literal},))]\n')
        import html
        expected = _literal(f'<{names["G"]}>{html.escape(value, quote=False)}</{names["G"]}>', style)
        return f'captured = {fn}({literal}, None)\nassert captured == {expected}\n'
    if mechanism == 'shell':
        return ('const capture = [];\nconst fake = {\n  exec: (...args) => capture.push(["exec", ...args]),\n'
                '  execFile: (...args) => capture.push(["execFile", ...args])\n};\n'
                f'{fn}({literal}, fake);\n'
                f'assert.deepEqual(capture, [["execFile", {_literal(names["C"], 0)}, [{literal}]]]);\n')
    if mechanism == 'sql':
        query = _literal(_fill(SQLQ, **names) + '?', 0)
        return ('const capture = [];\nconst fake = {get: (sql, params = []) => capture.push([sql, params])};\n'
                f'{fn}({literal}, fake);\n'
                f'assert.deepEqual(capture, [[{query}, [{literal}]]]);\n')
    markup, text = HTML_JS_FAKES[form]
    return (f'const capture = [];\nconst fake = {{\n  {markup},\n  {text}\n}};\n'
            f'{fn}({literal}, fake);\n'
            f'assert.deepEqual(capture, [["text", {literal}]]);\n')


def _mechanism_text(mechanism):
    old, new, summary, invariant, impact, scope, _fact = v1._mechanism(mechanism, 'python')
    return summary, invariant, impact, scope


def _family(partition, mechanism, language, k, global_index, spec=None):
    cfg = spec or PARTITIONS[partition]
    mech_index = MECHANISMS.index(mechanism)
    form = cfg['forms'][k]
    wrapper = cfg['wrappers'][(k + mech_index + LANGUAGES.index(language)) % len(cfg['wrappers'])]
    ordinal = LANGUAGES.index(language) * cfg['per_cell'] + k
    names = _names(cfg.get('vocab', partition), mechanism, language, ordinal)
    family = f'{cfg["tag"]}_{mechanism}_{"py" if language == "python" else "js"}_{k}'
    ext = 'py' if language == 'python' else 'js'
    path = f'fixtures/{family}.{ext}'
    sources, quotes = _sources(mechanism, language, form, wrapper, names)
    summary, invariant, impact, scope = _mechanism_text(mechanism)
    fact = _facts(mechanism, language, form, names)
    style = (ordinal + mech_index) % 2
    ordinary = ORDINARY[mechanism][ordinal % 4]
    boundary = BOUNDARY[mechanism][(ordinal + 1) % 4]
    tests = [
        {'purpose': 'Check ordinary input is preserved: it reaches the safe interface unchanged, using captures only.',
         'test_code': _test_code(mechanism, language, form, names, ordinary, style),
         'expected_observation': 'On the boundary-preserving version this assertion passes. No execution is claimed.',
         'execution_state': 'not_run'},
        {'purpose': 'Check boundary-breaking input stays inert: quotes, backslashes or newlines remain data, using captures only.',
         'test_code': _test_code(mechanism, language, form, names, boundary, 1 - style),
         'expected_observation': ('On the boundary-preserving version this assertion passes; on the vulnerable version the captured call differs and the assertion fails. '
                                  'No execution is claimed.'),
         'execution_state': 'not_run'}]
    acceptance = {'mechanism': mechanism, 'language': language, 'fn': names['fn'], 'ordinary': ordinary, 'boundary': boundary,
                  **{key: names[key] for key in ('C', 'T', 'K', 'G') if key in names}}
    return dict(partition=partition, mechanism=mechanism, language=language, family=family, path=path, form=form, wrapper=wrapper, acceptance=acceptance,
                names=names, sources=sources, quotes=quotes, summary=summary, invariant=invariant, impact=impact, scope=scope,
                fact=fact, tests=tests, global_index=global_index, cfg=cfg)


def _build_family_rows(f):
    family, path, language, v = f['family'], f['path'], f['language'], f['names']['v']
    cfg = f['cfg']
    files, revisions, descriptors = {}, {}, {}
    for side, source in f['sources'].items():
        raw = source.encode('utf-8')
        name = f'source/{family}/{side}.txt'
        files[name] = raw
        revisions[side] = hashlib.sha1(raw).hexdigest()
        descriptors[side] = {'path': name, 'sha256': _sha(raw), 'bytes': len(raw), 'source_path': path,
                             'revision': revisions[side], 'revision_kind': 'authored_content_identity'}
    rid = 'synthetic2:' + family
    common = {'repository_identity': f'authored-synthetic://security-trial-v2/{family}', 'family_id': family, 'issue_group_id': family,
              'language': language, 'review_status': 'authored_construction_oracle_not_independent_review',
              'revision_kind': 'authored_content_identity', 'synthetic': True}
    records = [{**common, 'record_id': rid, 'label_state': 'supported', 'relevant_parent_commit': revisions['vulnerable'],
                'fix_commit': revisions['fixed'], 'source_snapshots': _json([descriptors['vulnerable'], descriptors['fixed']])},
               {**common, 'record_id': rid + ':withheld', 'label_state': 'insufficient_context', 'relevant_parent_commit': revisions['withheld'],
                'fix_commit': None, 'source_snapshots': _json([descriptors['withheld']])}]
    views = []
    hidden = f['quotes']['withheld']
    for task, side in TASK_SIDES:
        status = {'vulnerable': 'finding', 'fixed': 'no_finding', 'withheld': 'insufficient_context'}[side]
        quote = f['quotes'][side]
        answer = {'status': status, 'findings': [], 'evidence': [{'quote': quote, 'reason': f['invariant'] if side == 'vulnerable' else (
            'The shown operation preserves the stated boundary.' if side == 'fixed' else 'The decisive adapter implementation is absent.')}],
            'assumptions': [], 'missing_context': [], 'validation_state': 'not_validated'}
        if side == 'vulnerable':
            answer['findings'] = [{'summary': f['summary'], 'location': f'{path}:{f["names"]["fn"]}', 'violated_invariant': f['invariant'],
                                   'attacker_control': f'{v} is untrusted user input.', 'impact': f['impact']}]
        elif side == 'fixed':
            answer['assumptions'] = ['Scoped conclusion for ' + f['scope'] + ' only; other risks are not assessed.']
        else:
            answer['missing_context'] = [f'Implementation of {f["names"]["api"]}.apply and its handling of untrusted {v} at the ' + f['scope'] + '.']
        context = f'[target] {path} ({language})\n{f["sources"][side]}\n[scope]\nReview only {f["scope"]}. {v} is untrusted user input.\n'
        context += (f['fact'] + '\n') if side != 'withheld' else f'The implementation and contract of {f["names"]["api"]}.apply are withheld.\n'
        if task == 'repair':
            answer['patch'] = ''.join(difflib.unified_diff(f['sources']['vulnerable'].splitlines(True), f['sources']['fixed'].splitlines(True),
                                                         fromfile='a/' + path, tofile='b/' + path, n=3))
            answer['behavioral_intent'] = (f'Preserve ordinary input, the {f["names"]["fn"]} signature and the surrounding control flow '
                                           f'while enforcing {f["scope"]}.')
        if task == 'regression_test_design':
            context += (f'Propose tests for the boundary-preserving candidate: one ordinary input and one boundary-breaking input. '
                        f'Inject only inert fake adapters; assume {f["names"]["fn"]} and assertion helpers are available. Tests are not run.\n')
            answer['tests'] = f['tests']
        pool = {'vulnerable': 'supported', 'fixed': 'scoped_negative', 'withheld': 'insufficient_context'}[side]
        source_text = f['sources'][side]
        views.append({'view_id': f'{family}:{task}:{side}', 'record_id': rid + (':withheld' if side == 'withheld' else ''),
                      'subject_id': f'{family}:{side}', 'family_id': family, 'issue_group_id': family, 'task': task, 'pool': pool,
                      'split': cfg['split'], 'val_role': cfg['val_role'], 'eligible': True, 'weight': 1.0, 'context_text': context,
                      'response_json': _json(answer), 'source_revision': revisions[side], 'source_normalized_sha256': _normalized(source_text),
                      'allowed_patch_paths': [path], 'oracle_fixed_sha256': descriptors['fixed']['sha256'],
                      'lineage_json': _json({'origin': 'authored_synthetic', 'template_family': family, 'side': side,
                                             'construction_oracle': f['mechanism'] + '_boundary_contrast_v2', 'human_review': False,
                                             'legacy_holdout_untouched': None, 'form_id': f['form'], 'wrapper_id': f['wrapper']}),
                      'partition_role': f['partition'], 'origin_corpus': 'security_trial_v2', 'mechanism': f['mechanism'], 'language': language,
                      'source_file': f'source/{family}/{side}.txt', 'source_sha256': descriptors[side]['sha256'],
                      'source_structural_sha256': structural_sha(source_text)})
    oracle = {'family_id': family, 'split': cfg['split'], 'partition_role': f['partition'], 'origin_corpus': 'security_trial_v2',
              'language': language, 'mechanism': f['mechanism'], 'source_path': path, 'form_id': f['form'], 'wrapper_id': f['wrapper'],
              'invariant': f['invariant'], 'vulnerable_expression': f['quotes']['vulnerable'], 'fixed_expression': f['quotes']['fixed'],
              'acceptance': f['acceptance'],
              'decisive_fact_withheld': f'{f["names"]["api"]}.apply implementation and boundary semantics',
              'source_sha256': {side: descriptors[side]['sha256'] for side in f['sources']},
              'oracle_kind': 'self_authored_construction', 'execution_state': 'not_run'}
    return records, views, files, oracle


def _legacy_rows():
    records, views, files, oracles = v1.build_rows()
    oracle_by_family = {o['family_id']: o for o in oracles}
    out_views, out_oracles = [], []
    for view in views:
        oracle = oracle_by_family[view['family_id']]
        role = 'train' if view['split'] == 'train' else 'legacy_regression'
        side = view['view_id'].rsplit(':', 1)[1]
        lineage = json.loads(view['lineage_json'])
        lineage['legacy_holdout_untouched'] = False if role == 'legacy_regression' else None
        raw = files[f'source/{view["family_id"]}/{side}.txt']
        out_views.append({**view, 'split': 'train' if role == 'train' else 'legacy', 'val_role': '' if role == 'train' else 'legacy_regression',
                          'lineage_json': _json(lineage), 'partition_role': role, 'origin_corpus': 'security_trial_v1',
                          'mechanism': oracle['mechanism'], 'language': oracle['language'], 'source_file': f'source/{view["family_id"]}/{side}.txt',
                          'source_sha256': _sha(raw), 'source_structural_sha256': structural_sha(raw.decode('utf-8'))})
    for oracle in oracles:
        out_oracles.append({**oracle, 'partition_role': 'train' if oracle['split'] == 'train' else 'legacy_regression',
                            'origin_corpus': 'security_trial_v1', 'form_id': 0, 'wrapper_id': None})
    return records, out_views, files, out_oracles


def build_corpus(*, validate=True, supplement=0):
    """All partitions: records, views, source bytes and construction oracles, deterministic.

    ``supplement`` 0 is the published v2 corpus, byte for byte. ``supplement`` 1 appends 24 training-only families after everything else, so every
    row of the other partitions, and every original training row, is unchanged."""
    if supplement not in (0, *SUPPLEMENTS):
        raise ValueError(f'Unknown training supplement: {supplement}')
    records, views, files, oracles = _legacy_rows()
    index = 0
    for partition in PARTITIONS:
        for mechanism in MECHANISMS:
            for language in LANGUAGES:
                for k in range(PARTITIONS[partition]['per_cell']):
                    family = _family(partition, mechanism, language, k, index)
                    r, v, f, o = _build_family_rows(family)
                    records += r
                    views += v
                    files.update(f)
                    oracles.append(o)
                    index += 1
    if supplement:
        spec = SUPPLEMENTS[supplement]
        for mechanism in MECHANISMS:
            for language in LANGUAGES:
                for k in range(spec['per_cell']):
                    family = _family('train', mechanism, language, k, index, spec=spec)
                    r, v, f, o = _build_family_rows(family)
                    records += r
                    views += v
                    files.update(f)
                    oracles.append(o)
                    index += 1
    corpus = {'records': records, 'views': views, 'files': files, 'oracles': oracles}
    if validate:
        validate_corpus(corpus, supplement=supplement)
    return corpus


def authored_pairs(corpus):
    """(source sha256, test sha256) for every authored side, to restrict what the host reference runner may execute."""
    pairs, tests = set(), {}
    for view in corpus['views']:
        if view['task'] == 'regression_test_design':
            tests[view['family_id']] = [t['test_code'] for t in json.loads(view['response_json'])['tests']]
    for view in corpus['views']:
        if view['task'] == 'regression_test_design' and view['view_id'].endswith(':fixed'):
            family = view['family_id']
            for side in ('vulnerable', 'fixed'):
                text = corpus['files'][f'source/{family}/{side}.txt'].decode('utf-8')
                for code in tests[family]:
                    pairs.add((hashlib.sha256(text.encode()).hexdigest(), hashlib.sha256(code.encode()).hexdigest()))
    return pairs


def reference_test_receipts(corpus):
    """Execute each new family's authored tests on authored sources (host, byte-identical allowlist)."""
    from training.finetune.reference_runner import ENVIRONMENT, run_reference
    allowed = authored_pairs(corpus)
    receipts = []
    for oracle in corpus['oracles']:
        if oracle['origin_corpus'] != 'security_trial_v2':
            continue
        family = oracle['family_id']
        view = next(v for v in corpus['views'] if v['view_id'] == f'{family}:regression_test_design:fixed')
        codes = [t['test_code'] for t in json.loads(view['response_json'])['tests']]
        per_side = {}
        for side in ('fixed', 'vulnerable'):
            source = corpus['files'][f'source/{family}/{side}.txt'].decode('utf-8')
            per_side[side] = [run_reference(source, code, language=oracle['language'], allowed=allowed)['outcome'] for code in codes]
        fixed = 'passed' if all(o == 'passed' for o in per_side['fixed']) else 'not_passed'
        vulnerable = ('failed_assertion' if 'failed_assertion' in per_side['vulnerable']
                      and all(o in ('passed', 'failed_assertion') for o in per_side['vulnerable']) else 'not_discriminating')
        receipts.append({'family_id': family, 'partition_role': oracle['partition_role'], 'language': oracle['language'],
                         'mechanism': oracle['mechanism'], 'execution_environment': ENVIRONMENT,
                         'test_sha256': [hashlib.sha256(c.encode()).hexdigest() for c in codes],
                         'fixed_outcomes': per_side['fixed'], 'vulnerable_outcomes': per_side['vulnerable'],
                         'fixed': fixed, 'vulnerable': vulnerable})
    return receipts


def _completion(view):
    from training.finetune.sft_serialization import render_completion
    return render_completion(view['task'], json.loads(view['response_json']))[0]


def estimate_tokens(view):
    completion = len(_completion(view)) / COMPLETION_CHARS_PER_TOKEN
    prompt = PROMPT_BASE_TOKENS + len(view['context_text']) / CONTEXT_CHARS_PER_TOKEN
    return {'completion': completion, 'prompt': prompt, 'total': completion + prompt}


def check_sizes(corpus, max_completion_tokens=None, max_total_tokens=MAX_TOTAL_TOKENS, tokenizer=None):
    """Reject examples whose target or total would not fit; nothing is truncated. Uses the pinned tokenizer when supplied."""
    limits = max_completion_tokens or MAX_NEW_TOKENS
    rejected, largest = [], {}
    for view in corpus['views']:
        if tokenizer is None:
            size = estimate_tokens(view)
        else:
            from training.finetune import security_serialization, tokenization
            serialized = security_serialization.serialize_example(view['task'], view['context_text'], json.loads(view['response_json']), tokenizer=tokenizer)
            encoded, rejection = tokenization.try_encode(tokenizer, serialized)
            if encoded is None:
                rejected.append({'view_id': view['view_id'], 'reason': rejection})
                continue
            size = {'completion': encoded.completion_tokens, 'prompt': encoded.prompt_tokens, 'total': len(encoded.input_ids)}
        largest[view['task']] = max(largest.get(view['task'], 0), round(size['completion']))
        if size['completion'] > limits[view['task']] * 0.8 or size['total'] > max_total_tokens:
            rejected.append({'view_id': view['view_id'], 'reason': 'oversized', 'completion': round(size['completion']), 'total': round(size['total'])})
    if rejected:
        raise ValueError('oversized or unencodable supervision rejected: ' + _json(rejected[:5]))
    return {'method': 'pinned_tokenizer' if tokenizer is not None else 'calibrated_estimate', 'rejected': [],
            'max_completion_tokens_by_task': largest, 'limits': limits, 'max_total_tokens': max_total_tokens}


def leakage_report(corpus):
    """Skeleton 3-gram Jaccard between every pair of sources in different partitions."""
    sources = {}
    for view in corpus['views']:
        sources[view['source_file']] = (view['partition_role'], _shingles(skeleton(corpus['files'][view['source_file']].decode('utf-8'))))
    items = sorted(sources.items())
    best, pairs, stub_best = 0.0, 0, 0.0
    worst = None
    for (name_a, (role_a, set_a)), (name_b, (role_b, set_b)) in itertools.combinations(items, 2):
        if role_a == role_b:
            continue
        score = len(set_a & set_b) / len(set_a | set_b)
        if name_a.endswith('/withheld.txt') or name_b.endswith('/withheld.txt'):
            stub_best = max(stub_best, score)       # one-line api.apply stubs: reported, not gated
            continue
        pairs += 1
        if score > best:
            best, worst = score, (name_a, name_b)
    exact = defaultdict(set)
    for view in corpus['views']:
        for key in ('family_id', 'source_revision', 'source_normalized_sha256', 'source_structural_sha256'):
            exact[(key, view[key])].add(view['partition_role'])
    return {'method': 'skeleton 3-gram Jaccard over identifier- and literal-insensitive tokens', 'limit': SIMILARITY_LIMIT,
            'pairs_compared': pairs, 'max_cross_partition_similarity': round(best, 4), 'most_similar_pair': list(worst) if worst else None,
            'max_including_withheld_stubs': round(max(best, stub_best), 4), 'sink_overlap': sink_overlap_report(corpus),
            'exact_identity_crossings': sum(len(roles) > 1 for roles in exact.values())}


def sink_overlap_report(corpus):
    """Overlap of the vulnerable sink idiom (identifier- and literal-insensitive skeleton) between partitions of new families."""
    sinks = defaultdict(set)
    for o in corpus['oracles']:
        key = (o['mechanism'], o['language'], structural_sha(o['vulnerable_expression']))
        sinks[o['partition_role']].add(key)
    count = lambda a, b: len(sinks[a] & sinks[b])
    return {'method': 'sink skeleton of the vulnerable expression, by mechanism and language',
            'vulnerable_sink_overlap': {'train_development': count('train', 'development'), 'train_blind': count('train', 'blind_test'),
                                        'development_blind': count('development', 'blind_test')},
            'legacy_regression_vs_train': count('legacy_regression', 'train'),
            'note': 'Fixed idioms are shared by design; legacy regression reuses the original form 0 and is reported, not gated.'}


def _core_shingles(quotes, n=6):
    shingles = set()
    for quote in quotes:
        tokens = skeleton(quote)
        while tokens and tokens[-1] in (';', ','):          # a trailing statement or argument separator is not part of the spelling
            tokens.pop()
        shingles |= {tuple(tokens[i:i + n]) for i in range(len(tokens) - n + 1)}
    return shingles


def supplement_core_overlap(corpus):
    """Skeleton 6-grams of a supplement family's vulnerable quote that also occur in a development or blind quote of the same mechanism and language.

    The statement-level sink gate hashes a whole quoted statement, so wrapping a held-out spelling in an assignment (``const q = "C ".concat(v);`` against the
    blind ``api.exec("C ".concat(v))``) changes the hash and slips through. This gate compares sub-expressions. A spelling the original training set already
    used is not counted, because it was accepted before the supplement existed. Returns {mechanism/language: [shared 6-grams]}; empty means clean."""
    prefixes = tuple(spec['tag'] + '_' for spec in SUPPLEMENTS.values())
    groups = defaultdict(lambda: {'base': [], 'new': [], 'held': []})
    for oracle in corpus['oracles']:
        key = (oracle['mechanism'], oracle['language'])
        if oracle['partition_role'] == 'train':
            groups[key]['new' if oracle['family_id'].startswith(prefixes) else 'base'].append(oracle['vulnerable_expression'])
        elif oracle['partition_role'] in ('development', 'blind_test'):
            groups[key]['held'].append(oracle['vulnerable_expression'])
    out = {}
    for key, group in sorted(groups.items()):
        shared = (_core_shingles(group['new']) - _core_shingles(group['base'])) & _core_shingles(group['held'])
        if shared:
            out['/'.join(key)] = sorted(' '.join(shingle) for shingle in shared)
    return out


def coverage_report(corpus):
    partitions = {}
    for role in ('train', 'development', 'blind_test', 'legacy_regression'):
        views = [v for v in corpus['views'] if v['partition_role'] == role]
        partitions[role] = {'views': len(views), 'families': len({v['family_id'] for v in views}),
                            'by_task': dict(Counter(v['task'] for v in views)),
                            'by_pool': dict(Counter(v['pool'] for v in views)),
                            'by_mechanism_language': {f'{m}/{l}': c for (m, l), c in sorted(Counter((v['mechanism'], v['language']) for v in views).items())},
                            'by_origin': dict(Counter(v['origin_corpus'] for v in views))}
    forms = defaultdict(set)
    for o in corpus['oracles']:
        forms[o['partition_role']].add((o['mechanism'], o['language'], o['form_id']))
    omitted = ['languages other than python and javascript', 'mechanisms other than shell, sql and html (no path traversal, deserialization, SSRF, crypto or authorization)',
               'real repositories or collected code (all sources are authored templates)', 'multi-file or cross-module context',
               'withheld-context cases other than an unseen api.apply', 'independent human review']
    seen = defaultdict(set)
    for role, items in forms.items():
        seen[role] = items
    unseen_dev = sorted(seen['development'] - seen['train'])
    unseen_blind = sorted(seen['blind_test'] - seen['train'])
    report = {'schema': 'security-trial-coverage/2', 'partitions': partitions, 'exclusions': [], 'omitted_strata': omitted,
              'forms_unseen_in_training': {'development': len(unseen_dev), 'blind_test': len(unseen_blind)},
              'form_overlap_with_training': {'development': len(seen['development'] & seen['train']), 'blind_test': len(seen['blind_test'] & seen['train'])}}
    if any(o['partition_role'] == 'train' and o['form_id'] >= 5 for o in corpus['oracles']):
        report['transfer_caveat'] = ('forms_unseen_in_training counts form ids only. Training supplement 1 adds prelude-plus-call and helper shapes that are '
                                     'structurally close to development form 3, so development no longer tests transfer to an unseen shape for them; the blind '
                                     'partition (form 4) is still never trained on.')
    return report


def validate_corpus(corpus, supplement=0):
    """Everything the handoff requires of a corpus, raising ValueError at the first violation."""
    from training.finetune import sft_contracts
    from training.finetune.repair_validation import assess_patch
    from training.finetune.security_serialization import parse_response
    records, views, files, oracles = corpus['records'], corpus['views'], corpus['files'], corpus['oracles']
    expected = {'train': 216 + 144 * supplement, 'development': 72, 'blind_test': 72, 'legacy_regression': 18}
    if Counter(v['partition_role'] for v in views) != expected or len(oracles) != 63 + 24 * supplement or len(records) != 126 + 48 * supplement:
        raise ValueError('Unexpected v2 corpus membership')
    if len({v['view_id'] for v in views}) != len(views):
        raise ValueError('Duplicate view identity')
    record_map = {r['record_id']: r for r in records}
    seen_roles = defaultdict(set)
    for view in views:
        lineage = json.loads(view['lineage_json'])
        if lineage.get('origin') != 'authored_synthetic' or lineage.get('human_review') is not False or view['eligible'] is not True or view['weight'] != 1.0:
            raise ValueError('Only authored synthetic origin is eligible')
        raw = files[view['source_file']]
        source = raw.decode('utf-8')
        record = record_map[view['record_id']]
        descriptor = [d for d in json.loads(record['source_snapshots']) if d['revision'] == view['source_revision']]
        if (len(descriptor) != 1 or descriptor[0]['sha256'] != _sha(raw) or descriptor[0]['revision'] != hashlib.sha1(raw).hexdigest()
                or view['source_sha256'] != _sha(raw) or view['source_normalized_sha256'] != _normalized(source) or source not in view['context_text']
                or view['source_structural_sha256'] != structural_sha(source)):
            raise ValueError('Source identity or context differs: ' + view['view_id'])
        response = json.loads(view['response_json'])
        if sft_contracts.validate_response(view['task'], response):
            raise ValueError('Invalid response schema: ' + view['view_id'])
        answer, problems = parse_response(view['task'], _completion(view))
        if answer is None:
            raise ValueError('Target is not strict JSON under the response contract: ' + view['view_id'] + ' ' + str(problems))
        if any(e['quote'] not in source for e in response['evidence']):
            raise ValueError('Evidence quote is absent from exact source bytes: ' + view['view_id'])
        if view['task'] == 'repair':
            path = view['allowed_patch_paths'][0]
            result = assess_patch(response['patch'], {path: source}, allowed_paths={path})
            if result['state'] != 'applies_exactly' or result['modified_sha256'] != {path: view['oracle_fixed_sha256']}:
                raise ValueError('Repair does not produce exact authored fixed source: ' + view['view_id'])
        for key in ('family_id', 'issue_group_id', 'source_revision', 'source_normalized_sha256', 'source_structural_sha256'):
            seen_roles[(key, view[key])].add(view['partition_role'])
    if any(len(roles) > 1 for roles in seen_roles.values()):
        raise ValueError('Cross-partition family, revision or source leakage')
    leak = leakage_report(corpus)
    if leak['max_cross_partition_similarity'] >= SIMILARITY_LIMIT:
        raise ValueError('Cross-partition structural similarity too high: ' + _json(leak['most_similar_pair']))
    if any(leak['sink_overlap']['vulnerable_sink_overlap'].values()):
        raise ValueError('A vulnerable sink idiom is shared between partitions: ' + _json(leak['sink_overlap']))
    if supplement and supplement_core_overlap(corpus):
        raise ValueError('A training supplement spelling is a development or blind sub-expression: ' + _json(supplement_core_overlap(corpus)))
    check_sizes(corpus)
    return leak


# -- publication -----------------------------------------------------------------------------------------------------------------------------------
MANIFEST = 'dataset_manifest.json'
BLIND_TAG = 'security_trial_v2_blind'
V1_MANIFEST_SHA256 = 'd0555e5600e941894ee46184cfee16242a9b707ce95be63befaadfd4cc120e0d'
MAIN_ROLES = ('train', 'development', 'legacy_regression')
PARTITION_META = {
    'train': {'split': 'train', 'in_training_bundle': True, 'untouched': None},
    'development': {'split': 'val', 'in_training_bundle': True, 'untouched': None},
    'legacy_regression': {'split': 'legacy', 'in_training_bundle': True, 'untouched': False},
    'blind_test': {'split': 'blind', 'in_training_bundle': False, 'untouched': True}}
REPORT_NAMES = ('coverage', 'leakage', 'size_audit', 'reference_validation')
MANIFEST_CONSTANTS = {
    'schema': SCHEMA, 'dataset_status': 'frozen', 'synthetic': True, 'independent_evaluation': False, 'origin': 'authored_synthetic',
    'label_authority': 'self_authored_construction_oracles', 'human_review': False, 'generated_code_execution': 'never',
    'reference_execution': 'host_trusted_authored_reference', 'collected_source_admission': 'not_called', 'protocol': 'security_chat_v2',
    'revision_kind': 'authored_content_identity'}


def _subset(corpus, roles):
    views = [v for v in corpus['views'] if v['partition_role'] in roles]
    families = {v['family_id'] for v in views}
    return {'views': views, 'records': [r for r in corpus['records'] if r['family_id'] in families],
            'files': {n: raw for n, raw in corpus['files'].items() if n.split('/')[1] in families},
            'oracles': [o for o in corpus['oracles'] if o['family_id'] in families]}


def _partitions(subset):
    out = {}
    for role in PARTITION_META:
        views = [v for v in subset['views'] if v['partition_role'] == role]
        if views:
            out[role] = {'views': len(views), 'families': len({v['family_id'] for v in views}), **PARTITION_META[role]}
    return out


def _redact(name):
    blind_tag = PARTITIONS['blind_test']['tag']
    return 'blind:' + hashlib.sha256(name.encode()).hexdigest()[:8] if blind_tag in name else name


def _expected_receipts(subset):
    """Test hashes a stored reference receipt must carry for each new family of the subset."""
    tests = {}
    for view in subset['views']:
        if view['task'] == 'regression_test_design' and view['origin_corpus'] == 'security_trial_v2' and view['view_id'].endswith(':fixed'):
            tests[view['family_id']] = [hashlib.sha256(t['test_code'].encode()).hexdigest() for t in json.loads(view['response_json'])['tests']]
    return tests


def _reports(corpus, subset, receipts, sealed):
    coverage = coverage_report(subset)
    families = set(_expected_receipts(subset))
    leak = leakage_report(corpus)
    if not sealed:
        leak['most_similar_pair'] = [_redact(n) for n in leak['most_similar_pair'] or []]
        for key in ('partitions', 'forms_unseen_in_training', 'form_overlap_with_training'):
            coverage[key].pop('blind_test', None)
        coverage['sealed_partitions'] = {'blind_test': {'views': 72, 'families': 12}}
    return {'coverage': coverage, 'leakage': leak, 'size_audit': check_sizes(subset),
            'reference_validation': {'schema': 'security-reference-validation/1', 'execution_environment': 'host_trusted_authored_reference',
                                     'generated_code_execution': 'never', 'receipts': [r for r in receipts if r['family_id'] in families]}}


def _limits_for(supplement):
    return LIMITS + ([SUPPLEMENT_LIMITS[supplement]] if supplement else [])


def _write_publication(root, subset, manifest_fields, reports, limits=None):
    import pyarrow as pa
    import pyarrow.parquet as pq
    limits = LIMITS if limits is None else limits
    root.mkdir(parents=True, exist_ok=False)
    for name, raw in subset['files'].items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    pq.write_table(pa.Table.from_pylist(subset['records']), root / 'records.parquet', compression='NONE')
    pq.write_table(pa.Table.from_pylist(subset['views']), root / 'views.parquet', compression='NONE')
    (root / 'construction_oracles.json').write_bytes((_json(subset['oracles']) + '\n').encode())
    (root / 'LIMITATIONS.txt').write_bytes(('\n'.join(limits) + '\n').encode())
    (root / 'reports').mkdir()
    for name, body in reports.items():
        (root / 'reports' / f'{name}.json').write_bytes((_json(body) + '\n').encode())
    inventory = {p.relative_to(root).as_posix(): {'sha256': _sha(p.read_bytes()), 'bytes': p.stat().st_size}
                 for p in sorted(root.rglob('*')) if p.is_file()}
    families = len({v['family_id'] for v in subset['views']})
    manifest = {**MANIFEST_CONSTANTS, 'derived_from': {'tag': v1.TAG, 'manifest_sha256': V1_MANIFEST_SHA256}, 'limitations': limits,
                'partitions': _partitions(subset), 'families': families,
                'counts': {'records': len(subset['records']), 'views': len(subset['views']), 'families': families},
                'reports': {name: f'reports/{name}.json' for name in REPORT_NAMES}, **manifest_fields, 'files': inventory}
    (root / MANIFEST).write_bytes((_json(manifest) + '\n').encode())
    return manifest


def _main_partitions(subset):
    return {**_partitions(subset), 'blind_test': {'views': 72, 'families': 12, **PARTITION_META['blind_test']}}


def _blind_ids_sha(subset):
    return hashlib.sha256('\n'.join(sorted(v['view_id'] for v in subset['views'])).encode()).hexdigest()


def publish(main_dir, blind_dir, *, receipts=None, supplement=0):
    """Create the immutable main publication and the sealed blind publication; never overwrites either.

    With a training supplement the main publication carries its own tag and limitations, while the sealed blind publication is the same bytes as
    without it (same commitment), so results before and after the supplement are scored on identical development, legacy and blind views."""
    main_dir, blind_dir = Path(main_dir), Path(blind_dir)
    for root in (main_dir, blind_dir):
        if root.exists():
            raise FileExistsError('Refusing to reuse a dataset publication: ' + str(root))
    corpus = build_corpus(supplement=supplement)
    receipts = receipts if receipts is not None else reference_test_receipts(corpus)
    failing = [r for r in receipts if r['fixed'] != 'passed' or r['vulnerable'] != 'failed_assertion']
    if failing:                      # refuse before writing anything; a timeout on a loaded host reads differently from a real assertion failure
        raise ValueError('Reference tests did not pass the fixed source and fail the vulnerable one (rerun on an unloaded machine if an outcome is a timeout): '
                         + _json([{k: r.get(k) for k in ('family_id', 'fixed_outcomes', 'vulnerable_outcomes')} for r in failing[:5]]))
    blind, main = _subset(corpus, ('blind_test',)), _subset(corpus, MAIN_ROLES)
    # The sealed publication must not depend on the training supplement, or its commitment would change: its separation report comes from the plain corpus.
    plain = corpus if not supplement else build_corpus(supplement=0)
    blind_manifest = _write_publication(blind_dir, blind, {'tag': BLIND_TAG, 'publication_role': 'sealed_blind_test'},
                                        _reports(plain, blind, receipts, sealed=True))
    commitment = {'tag': BLIND_TAG, 'sealed': True, 'views': len(blind['views']), 'families': blind_manifest['families'],
                  'manifest_sha256': _sha((blind_dir / MANIFEST).read_bytes()), 'view_ids_sha256': _blind_ids_sha(blind)}
    fields = {'tag': SUPPLEMENT_TAG if supplement else TAG, 'publication_role': 'main', 'blind_commitment': commitment, 'partitions': _main_partitions(main)}
    if supplement:
        spec = SUPPLEMENTS[supplement]
        fields['supplement'] = {'number': supplement, 'families': len(MECHANISMS) * len(LANGUAGES) * spec['per_cell'], 'forms': list(spec['forms'])}
    return _write_publication(main_dir, main, fields, _reports(corpus, main, receipts, sealed=False), limits=_limits_for(supplement))


def _verify(path, *, roles, role_name):
    import pyarrow.parquet as pq
    from pathlib import PurePosixPath
    path = Path(path)
    root = path.parent if path.name == MANIFEST else path
    manifest = json.loads((root / MANIFEST).read_bytes())
    supplement = SUPPLEMENT_TAGS.get(manifest.get('tag')) if role_name == 'main' else 0       # the tag says which deterministic variant to regenerate
    if supplement is None:
        raise ValueError('Unknown security-trial-dataset/2 tag: ' + str(manifest.get('tag')))
    corpus = build_corpus(supplement=supplement)
    expected = _subset(corpus, roles)
    limits = _limits_for(supplement)
    if (any(manifest.get(k) != value for k, value in MANIFEST_CONSTANTS.items()) or manifest.get('publication_role') != role_name
            or manifest.get('limitations') != limits or manifest.get('partitions') != (_main_partitions(expected) if role_name == 'main' else _partitions(expected))
            or manifest.get('supplement') != ({'number': supplement, 'families': 24, 'forms': list(SUPPLEMENTS[supplement]['forms'])} if supplement else None)
            or manifest.get('derived_from') != {'tag': v1.TAG, 'manifest_sha256': V1_MANIFEST_SHA256}
            or manifest.get('counts') != {'records': len(expected['records']), 'views': len(expected['views']), 'families': manifest.get('families')}):
        raise ValueError('Invalid security-trial-dataset/2 manifest')
    inventory = manifest.get('files')
    if not isinstance(inventory, dict) or not 4 <= len(inventory) <= 600:
        raise ValueError('Invalid dataset inventory')
    actual = {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()}
    if actual != set(inventory) | {MANIFEST}:
        raise ValueError('Dataset inventory differs')
    files = {}
    for name, identity in inventory.items():
        rel = PurePosixPath(name)
        if not name or rel.is_absolute() or '\\' in name or ':' in name or '..' in rel.parts:
            raise ValueError('Unsafe dataset inventory path')
        member = root.joinpath(*rel.parts)
        if member.is_symlink() or not member.is_file() or any(parent.is_symlink() for parent in member.parents if parent.is_relative_to(root)):
            raise ValueError('Unsafe dataset inventory member')
        raw = member.read_bytes()
        if len(raw) > 4 << 20 or identity != {'sha256': _sha(raw), 'bytes': len(raw)}:
            raise ValueError('Dataset file identity differs: ' + name)
        files[name] = raw
    data_members = set(expected['files']) | {'records.parquet', 'views.parquet', 'construction_oracles.json', 'LIMITATIONS.txt'} | \
        {f'reports/{n}.json' for n in REPORT_NAMES}
    if set(inventory) != data_members or manifest.get('reports') != {n: f'reports/{n}.json' for n in REPORT_NAMES}:
        raise ValueError('Dataset inventory differs from authored membership')
    records = pq.read_table(root / 'records.parquet').to_pylist()
    views = pq.read_table(root / 'views.parquet').to_pylist()
    if (records != expected['records'] or views != expected['views'] or json.loads(files['construction_oracles.json']) != expected['oracles']
            or files['LIMITATIONS.txt'] != ('\n'.join(limits) + '\n').encode()
            or any(files.get(n) != raw for n, raw in expected['files'].items())):
        raise ValueError('Corpus differs from the authored construction oracle')
    reference = json.loads(files['reports/reference_validation.json'])
    tests = _expected_receipts(expected)
    if (reference.get('execution_environment') != 'host_trusted_authored_reference' or reference.get('generated_code_execution') != 'never'
            or {r['family_id'] for r in reference['receipts']} != set(tests) or len(reference['receipts']) != len(tests)
            or any(r['fixed'] != 'passed' or r['vulnerable'] != 'failed_assertion' or r['test_sha256'] != tests[r['family_id']] for r in reference['receipts'])):
        raise ValueError('Reference validation receipts are missing, stale or failing')
    if role_name == 'main':
        commitment = manifest.get('blind_commitment') or {}
        blind = _subset(corpus, ('blind_test',))
        if (commitment.get('tag') != BLIND_TAG or commitment.get('sealed') is not True or commitment.get('views') != 72
                or commitment.get('families') != 12 or commitment.get('view_ids_sha256') != _blind_ids_sha(blind)
                or len(str(commitment.get('manifest_sha256'))) != 64):
            raise ValueError('Invalid blind commitment')
        if any(PARTITIONS['blind_test']['tag'] in n for n in inventory) or any(v['partition_role'] == 'blind_test' for v in views):
            raise ValueError('Blind material found in the main publication')
        if PARTITIONS['blind_test']['tag'] in files['reports/leakage.json'].decode('utf-8'):
            raise ValueError('Blind identity leaked into the main report')
        leak = json.loads(files['reports/leakage.json'])
        if leak['max_cross_partition_similarity'] >= SIMILARITY_LIMIT or leak['exact_identity_crossings']:
            raise ValueError('Separation report fails')
    return manifest


def verify_dataset(path):
    """Authenticate the main publication against the deterministic generator, including the blind commitment."""
    return _verify(path, roles=MAIN_ROLES, role_name='main')


def verify_blind(path, *, commitment=None):
    """Authenticate the sealed blind publication, optionally against the commitment the main manifest carries."""
    path = Path(path)
    root = path.parent if path.name == MANIFEST else path
    manifest = _verify(root, roles=('blind_test',), role_name='sealed_blind_test')
    digest = _sha((root / MANIFEST).read_bytes())
    if commitment is not None and (commitment.get('manifest_sha256') != digest or commitment.get('views') != manifest['counts']['views']):
        raise ValueError('Blind publication does not match the commitment in the main manifest')
    return {'views': manifest['counts']['views'], 'families': manifest['families'], 'manifest_sha256': digest}


def read_dataset(path):
    """Verified rows of a security-trial-dataset/1 or /2 publication; the /1 verifier and its bytes are unchanged."""
    import pyarrow.parquet as pq
    path = Path(path)
    root = path.parent if path.name == MANIFEST else path
    head = json.loads((root / MANIFEST).read_bytes())
    if head.get('schema') == v1.SCHEMA:
        manifest = v1.verify_dataset(root)
    elif head.get('schema') == SCHEMA and head.get('publication_role') == 'sealed_blind_test':
        verify_blind(root)
        manifest = head
    elif head.get('schema') == SCHEMA:
        manifest = verify_dataset(root)
    else:
        raise ValueError('Unsupported dataset schema: ' + str(head.get('schema')))
    return {'schema': manifest['schema'], 'manifest': manifest, 'views': pq.read_table(root / 'views.parquet').to_pylist(),
            'records': pq.read_table(root / 'records.parquet').to_pylist()}
