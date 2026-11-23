"""security_chat_v3: the grounded v35 prompt protocol. The response contract (keys, strict JSON, validators) is exactly security_chat_v2's; the prompt differs.

What changes, and why (see experiments/grounded_eval_v34/results/3c3e75c0fccdbf66/RESULTS_RUN.md):

* Every prompt carries the closed catalogue of weakness classes as the allowed values of ``violated_invariant``. A class is chosen from the code, not written from a
  scope line, and a class that was never a training positive (SSRF, code injection) is still on the list, so choosing it is possible and its use is measurable.
* The scope line is one of three forms (any class, one class, a set of classes) and the answer follows from the code and the scope, never from the form.
* The code is numbered, so an explanation can cite lines and a repair can name the line it changes.
* A repair is an edit list (``training.finetune.repair_edits``): the lines to remove and the lines to add, with the number of the first removed line. The
  converter, not the model, produces context and counts. The ``patch`` field still holds one non-empty string, so the v2 validators accept it unchanged.
"""
from __future__ import annotations

import copy
import hashlib
import json

from training.finetune import security_serialization as v2, serialization
from training.finetune.sft_serialization import Serialized, render_completion

PROTOCOL = 'security_chat_v3'
TASKS = v2.TASKS
CHAT_SYSTEM = v2.CHAT_SYSTEM

# (id, title): ids sorted so the order carries no hint; titles are the public MITRE CWE names (static catalogue, provenance "cwe_catalog").
CATALOGUE = (
    ('CWE-22', 'Improper Limitation of a Pathname to a Restricted Directory (Path Traversal)'),
    ('CWE-78', 'Improper Neutralization of Special Elements used in an OS Command (OS Command Injection)'),
    ('CWE-79', 'Improper Neutralization of Input During Web Page Generation (Cross-site Scripting)'),
    ('CWE-89', 'Improper Neutralization of Special Elements used in an SQL Command (SQL Injection)'),
    ('CWE-94', 'Improper Control of Generation of Code (Code Injection)'),
    ('CWE-113', 'Improper Neutralization of CRLF Sequences in HTTP Headers (HTTP Response Splitting)'),
    ('CWE-200', 'Exposure of Sensitive Information to an Unauthorized Actor'),
    ('CWE-295', 'Improper Certificate Validation'),
    ('CWE-327', 'Use of a Broken or Risky Cryptographic Algorithm'),
    ('CWE-352', 'Cross-Site Request Forgery (CSRF)'),
    ('CWE-400', 'Uncontrolled Resource Consumption'),
    ('CWE-502', 'Deserialization of Untrusted Data'),
    ('CWE-601', 'URL Redirection to Untrusted Site (Open Redirect)'),
    ('CWE-611', 'Improper Restriction of XML External Entity Reference'),
    ('CWE-862', 'Missing Authorization'),
    ('CWE-918', 'Server-Side Request Forgery (SSRF)'),
    ('CWE-1321', 'Improperly Controlled Modification of Object Prototype Attributes (Prototype Pollution)'),
    ('CWE-1333', 'Inefficient Regular Expression Complexity'),
    ('CWE-1336', 'Improper Neutralization of Special Elements Used in a Template Engine'),
)
CATALOGUE_TITLES = tuple(title for _, title in CATALOGUE)
TITLE_OF = dict(CATALOGUE)
# Classes whose analyser labels are normalised to a catalogue class (CWE-77 and CWE-78 share their sinks, and two near-identical titles would make the class choice arbitrary).
CLASS_ALIASES = {'CWE-77': 'CWE-78'}

TASK_TEXT = dict(v2.TASK_TEXT)
TASK_TEXT['discovery'] = (
    'Review the supplied target for security vulnerabilities supported by the shown code. The code is numbered; the numbers are not part of the code. '
    'The [scope] line says what to review: any class, one class, or a set of classes. Report a finding only for a flaw of a class in scope that the shown code '
    'supports. If the code holds no such flaw, answer no_finding for the scope. For a finding, violated_invariant is exactly one title from the schema list, '
    'chosen from what the code does; summary and evidence reason describe the data flow in the shown code with its line numbers.')
TASK_TEXT['repair'] = (
    'Propose a minimal edit that addresses the supported flaw while preserving legitimate behavior. The code is numbered; the numbers are not part of the code. '
    'For a finding, violated_invariant is exactly one title from the schema list, and patch and behavioral_intent are non-whitespace strings. patch is plain text made of one or more edits in ascending order. Each edit starts '
    'with a line "@@ N", where N is the number of the first line it removes (for a pure insertion, the number of the line it is inserted before). Then come the '
    'removed lines, each written as "-" followed by the exact line including its indentation, then the added lines, each written as "+" followed by the line. '
    'Write no context lines, no file headers and no counts. Do not wrap the patch in Markdown fences. For no_finding or insufficient_context, patch and '
    'behavioral_intent are null. A proposed patch is a candidate for human review and has not been applied or tested.')
RESPONSE_RULES = (v2.RESPONSE_RULES + ' For a finding, violated_invariant is required and is exactly one of the titles listed in the schema.')


def _schema(task):
    schema = copy.deepcopy(v2.RESPONSE_SCHEMAS[task])
    schema['properties']['findings']['items']['properties']['violated_invariant'] = {'type': 'string', 'enum': list(CATALOGUE_TITLES)}
    then = schema['allOf'][0]['then']['properties']['findings']
    items = then.setdefault('items', {})
    items['required'] = sorted(set(items.get('required', [])) | {'violated_invariant'})
    return schema


RESPONSE_SCHEMAS = {name: _schema(name) for name in v2.RESPONSE_SCHEMAS}


def messages(task, context_text):
    if task not in TASKS:
        raise ValueError('unknown security task: ' + str(task))
    instruction = TASK_TEXT[task]
    if TASKS[task].kind == 'json':
        instruction += '\n\n' + RESPONSE_RULES + '\n\nResponse JSON Schema:\n' + json.dumps(RESPONSE_SCHEMAS[task], ensure_ascii=True, separators=(',', ':'))
    return [{'role': 'system', 'content': CHAT_SYSTEM}, {'role': 'user', 'content': instruction + '\n\nSupplied source context:\n' + context_text}]


def render_prompt(task, context_text, *, tokenizer):
    if not getattr(tokenizer, 'chat_template', None) or not callable(getattr(tokenizer, 'apply_chat_template', None)):
        raise ValueError('security_chat_v3 requires the pinned tokenizer chat template')
    return tokenizer.apply_chat_template(messages(task, context_text), tokenize=False, add_generation_prompt=True)


def serialize_example(task, context_text, response, *, tokenizer):
    prompt = render_prompt(task, context_text, tokenizer=tokenizer)
    completion, spans = render_completion(task, response)
    return Serialized(task, PROTOCOL, prompt, completion, prompt + completion, len(prompt),
                      {key: (a + len(prompt), b + len(prompt)) for key, (a, b) in spans.items()}, serialization._reserved(context_text))


def parse_response(task, text):
    """The v2 contract, unchanged: catalogue conformance of violated_invariant is measured by the report, not enforced here."""
    return v2.parse_response(task, text)


def serialization_hash(tokenizer):
    render_prompt('discovery', '', tokenizer=tokenizer)
    body = {'id': PROTOCOL, 'system': CHAT_SYSTEM, 'chat_template': tokenizer.chat_template, 'task_text': TASK_TEXT, 'response_rules': RESPONSE_RULES,
            'response_schemas': RESPONSE_SCHEMAS, 'catalogue': [list(item) for item in CATALOGUE], 'aliases': CLASS_ALIASES,
            'field_groups': v2.contracts.FIELD_GROUP, 'tasks': {name: [task.id, task.kind, list(task.keys)] for name, task in TASKS.items()}}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
