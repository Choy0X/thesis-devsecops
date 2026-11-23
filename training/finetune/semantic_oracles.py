"""Trusted acceptance oracles that check behaviour, not a template.

The authored reference tests teach one exact call shape. The acceptance oracles here are what a *verifier* may judge a repair by, so they
accept every repair that is semantically right and reject the ones that are not:

* shell: the effective argument vector must equal ``[command, value]`` whether it is passed as a list (without a shell) or as a quoted
  command string (parsed with POSIX word splitting; unquoted metacharacters make it unparsable, which fails);
* SQL: ordinary input must yield the same effective query as before; boundary input must be bound as a parameter sequence ``[value]`` and
  must not appear in the SQL text;
* HTML: ordinary text is unchanged; boundary text must be inert (no ``<`` or ``>`` survives) and decode back to the original, or be set as
  text.

The ordinary oracle passes on the unmodified vulnerable source, so it measures preservation. The boundary oracle fails on it with an
assertion. Values travel as JSON string literals, valid in both Python and JavaScript.
"""
from __future__ import annotations

import json

SHELL_PY = r'''import shlex
calls = []
def fake(*args, **kwargs):
    calls.append((args, kwargs))
<<FN>>(<<VALUE>>, fake)
assert len(calls) == 1
args, kwargs = calls[0]
command = args[0]
if isinstance(command, (list, tuple)):
    assert not kwargs.get("shell"), "a list command must not run through a shell"
    argv = list(command)
else:
    try:
        argv = shlex.split(command)
    except ValueError:
        argv = None
assert argv == [<<C>>, <<VALUE>>]
'''
SQL_PY_HEAD = r'''calls = []
class Fake:
    def execute(self, sql, params=()):
        calls.append((sql, params))
<<FN>>(<<VALUE>>, Fake())
assert len(calls) == 1
sql, params = calls[0]
bound = [params] if isinstance(params, str) else list(params)
'''
SQL_PY_ORDINARY = r'''effective = sql
for item in bound:
    effective = effective.replace("?", "'" + str(item).replace("'", "''") + "'", 1)
assert effective == <<PREFIX>> + "'" + <<VALUE>>.replace("'", "''") + "'"
'''
SQL_PY_BOUNDARY = r'''assert bound == [<<VALUE>>], "the value must be bound, not spliced into the SQL text"
assert <<VALUE>> not in sql
'''
HTML_PY_HEAD = r'''import html
out = <<FN>>(<<VALUE>>, None)
'''
HTML_PY_ORDINARY = 'assert out == <<OPEN>> + <<VALUE>> + <<CLOSE>>\n'
HTML_PY_BOUNDARY = r'''assert out.startswith(<<OPEN>>) and out.endswith(<<CLOSE>>)
inner = out[len(<<OPEN>>):len(out) - len(<<CLOSE>>)]
assert "<" not in inner and ">" not in inner
assert html.unescape(inner) == <<VALUE>>
'''
SHELL_JS = r'''const calls = [];
const record = (kind) => (...args) => { calls.push([kind, ...args]); };
const fake = { exec: record("shell"), execSync: record("shell"), execFile: record("file"), execFileSync: record("file"),
               spawn: record("file"), spawnSync: record("file") };
function splitWords(text) {
  const words = []; let current = null; let i = 0;
  while (i < text.length) {
    const ch = text[i];
    if (ch === " " || ch === "\t" || ch === "\n") { if (current !== null) { words.push(current); current = null; } i++; continue; }
    if (ch === "'") { const end = text.indexOf("'", i + 1); if (end < 0) return null; current = (current || "") + text.slice(i + 1, end); i = end + 1; continue; }
    if (ch === '"') {
      let j = i + 1; let part = "";
      while (j < text.length && text[j] !== '"') {
        if (text[j] === "$" || text[j] === "`") return null;
        if (text[j] === "\\" && j + 1 < text.length) j++;
        part += text[j]; j++;
      }
      if (j >= text.length) return null;
      current = (current || "") + part; i = j + 1; continue;
    }
    if (ch === "\\" && i + 1 < text.length) { current = (current || "") + text[i + 1]; i += 2; continue; }
    if (";|&<>$`()".includes(ch)) return null;
    current = (current || "") + ch; i++;
  }
  if (current !== null) words.push(current);
  return words;
}
<<FN>>(<<VALUE>>, fake);
assert.strictEqual(calls.length, 1);
const [kind, first, second] = calls[0];
const argv = kind === "file" ? [first, ...(second || [])] : splitWords(first);
assert.deepStrictEqual(argv, [<<C>>, <<VALUE>>]);
'''
SQL_JS_HEAD = r'''const calls = [];
const fake = { get: (sql, params = []) => { calls.push([sql, params]); } };
<<FN>>(<<VALUE>>, fake);
assert.strictEqual(calls.length, 1);
const [sql, params] = calls[0];
const bound = Array.isArray(params) ? params : [params];
'''
SQL_JS_ORDINARY = r'''let effective = sql;
for (const item of bound) { effective = effective.replace("?", () => "'" + String(item).replace(/'/g, "''") + "'"); }
assert.strictEqual(effective, <<PREFIX>> + "'" + <<VALUE>>.replace(/'/g, "''") + "'");
'''
SQL_JS_BOUNDARY = r'''assert.deepStrictEqual(bound, [<<VALUE>>]);
assert.ok(!sql.includes(<<VALUE>>));
'''
HTML_JS_HEAD = r'''const calls = [];
const markup = (v) => { calls.push(["markup", v]); };
const text = (v) => { calls.push(["text", v]); };
const fake = {
  set innerHTML(v) { markup(v); }, set outerHTML(v) { markup(v); }, set textContent(v) { text(v); },
  insertAdjacentHTML(position, v) { markup(v); }, write(v) { markup(v); }, append(v) { text(v); }, writeText(v) { text(v); }
};
<<FN>>(<<VALUE>>, fake);
assert.strictEqual(calls.length, 1);
const [kind, rendered] = calls[0];
'''
HTML_JS_ORDINARY = 'assert.strictEqual(rendered, <<VALUE>>);\n'
HTML_JS_BOUNDARY = r'''if (kind === "text") {
  assert.strictEqual(rendered, <<VALUE>>);
} else {
  assert.ok(!rendered.includes("<") && !rendered.includes(">"));
  const decoded = rendered.replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, "&");
  assert.strictEqual(decoded, <<VALUE>>);
}
'''


def _fill(template, **names):
    for key, value in names.items():
        template = template.replace(f'<<{key}>>', value)
    return template


def _code(meta, kind):
    value = json.dumps(meta[kind])
    fn, mechanism, language = meta['fn'], meta['mechanism'], meta['language']
    common = {'FN': fn, 'VALUE': value}
    if mechanism == 'shell':
        return _fill(SHELL_PY if language == 'python' else SHELL_JS, C=json.dumps(meta['C']), **common)
    if mechanism == 'sql':
        prefix = json.dumps(f'SELECT id FROM {meta["T"]} WHERE {meta["K"]} = ')
        head, ordinary, boundary = ((SQL_PY_HEAD, SQL_PY_ORDINARY, SQL_PY_BOUNDARY) if language == 'python' else (SQL_JS_HEAD, SQL_JS_ORDINARY, SQL_JS_BOUNDARY))
        return _fill(head + (ordinary if kind == 'ordinary' else boundary), PREFIX=prefix, **common)
    opening, closing = json.dumps(f'<{meta["G"]}>'), json.dumps(f'</{meta["G"]}>')
    head, ordinary, boundary = ((HTML_PY_HEAD, HTML_PY_ORDINARY, HTML_PY_BOUNDARY) if language == 'python' else (HTML_JS_HEAD, HTML_JS_ORDINARY, HTML_JS_BOUNDARY))
    return _fill(head + (ordinary if kind == 'ordinary' else boundary), OPEN=opening, CLOSE=closing, **common)


def acceptance_tests(meta):
    """Ordinary-input preservation and boundary-breaking input, in this order."""
    return [{'name': kind, 'code': _code(meta, kind)} for kind in ('ordinary', 'boundary')]
