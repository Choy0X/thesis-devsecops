"""security_chat_v5: security_chat_v4 with the class catalogue of plan v6 (the 37 classes of v4 first and unchanged, then memory-safety, concurrency and panic classes).

Everything but the catalogue is v3's: the chat system text, the task text, the response rules and contract, the edit-format repairs and the numbered code. The
catalogue is read from catalogue_v5.json (written by tools.grounded_catalogue_v5): the classes of v4 first and unchanged, then the further classes the merged stores
support. A class that was never a training positive (SSRF, code injection) is still on the list.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from training.finetune import security_serialization as v2, serialization
from training.finetune import security_serialization_v3 as v3
from training.finetune.sft_serialization import Serialized, render_completion

PROTOCOL = 'security_chat_v5'
TASKS = v3.TASKS
CHAT_SYSTEM = v3.CHAT_SYSTEM
TASK_TEXT = v3.TASK_TEXT
RESPONSE_RULES = v3.RESPONSE_RULES

_FROZEN = json.loads((Path(__file__).with_name('catalogue_v5.json')).read_text(encoding='utf-8'))
CATALOGUE = tuple((cwe, title) for cwe, title in _FROZEN['catalogue'])
CATALOGUE_TITLES = tuple(title for _, title in CATALOGUE)
TITLE_OF = dict(CATALOGUE)
CLASS_ALIASES = dict(_FROZEN['aliases'])
HELD_OUT = tuple(_FROZEN['held_out'])
STATUS = _FROZEN.get('status', 'final')          # a provisional catalogue is for code and tests only; the build and the trial packager refuse it


def _schema(task):
    schema = copy.deepcopy(v3.RESPONSE_SCHEMAS[task])
    schema['properties']['findings']['items']['properties']['violated_invariant'] = {'type': 'string', 'enum': list(CATALOGUE_TITLES)}
    return schema


RESPONSE_SCHEMAS = {name: _schema(name) for name in v3.RESPONSE_SCHEMAS}


def messages(task, context_text):
    if task not in TASKS:
        raise ValueError('unknown security task: ' + str(task))
    instruction = TASK_TEXT[task]
    if TASKS[task].kind == 'json':
        instruction += '\n\n' + RESPONSE_RULES + '\n\nResponse JSON Schema:\n' + json.dumps(RESPONSE_SCHEMAS[task], ensure_ascii=True, separators=(',', ':'))
    return [{'role': 'system', 'content': CHAT_SYSTEM}, {'role': 'user', 'content': instruction + '\n\nSupplied source context:\n' + context_text}]


def render_prompt(task, context_text, *, tokenizer):
    if not getattr(tokenizer, 'chat_template', None) or not callable(getattr(tokenizer, 'apply_chat_template', None)):
        raise ValueError('security_chat_v5 requires the pinned tokenizer chat template')
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
