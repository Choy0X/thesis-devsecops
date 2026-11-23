"""Versioned native-chat SFT serialization; historical protocols are unchanged."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from training.finetune import serialization as historical, sft_contracts as contracts

PROTOCOL = 'sft_native_chat_v1'


@dataclass(frozen=True)
class Serialized(historical.Serialized):
    def owner(self, position):
        if position < self.boundary:
            return None
        for key, (start, end) in self.spans.items():
            if start <= position < end:
                return contracts.FIELD_GROUP[key]
        return historical.STRUCTURE


def messages(task, context_text):
    contracts.by_name(task)
    return [{'role': 'system', 'content': contracts.CHAT_SYSTEM},
            {'role': 'user', 'content': contracts.TASK_TEXT[task] +
             '\n\nSupplied source context:\n' + context_text}]


def render_prompt(task, context_text, *, tokenizer):
    if not getattr(tokenizer, 'chat_template', None) or not callable(getattr(tokenizer, 'apply_chat_template', None)):
        raise ValueError('sft_native_chat_v1 requires the pinned tokenizer chat template')
    return tokenizer.apply_chat_template(messages(task, context_text), tokenize=False, add_generation_prompt=True)


def render_completion(task, response):
    problems = contracts.validate_response(task, response)
    if problems:
        raise ValueError('; '.join(problems))
    definition = contracts.by_name(task)
    if definition.kind == 'text':
        return response, {'text': (0, len(response))}
    normalized = contracts.normalize(task, response)
    text, spans = '{', {}
    for index, key in enumerate(definition.keys):
        text += ('' if index == 0 else ', ') + json.dumps(key) + ': '
        value = json.dumps(normalized[key], ensure_ascii=False, separators=(', ', ': '))
        spans[key] = (len(text), len(text) + len(value))
        text += value
    return text + '}', spans


def serialize_example(task, context_text, response, *, tokenizer):
    prompt = render_prompt(task, context_text, tokenizer=tokenizer)
    completion, spans = render_completion(task, response)
    if not completion or (completion[0].isspace() and task not in contracts.CORPUS_TASKS):
        raise ValueError('SFT completion must start with a non-whitespace character')
    return Serialized(task, PROTOCOL, prompt, completion, prompt + completion, len(prompt),
                      {key: (start + len(prompt), end + len(prompt)) for key, (start, end) in spans.items()},
                      historical._reserved(context_text))


def parse_response(task, text):
    definition = contracts.by_name(task)
    if definition.kind == 'text':
        problems = contracts.validate_response(task, text)
        return (None, problems) if problems else (text, [])
    if not text.startswith('{'):
        return None, ['the answer must start with the opening brace']
    try:
        result, _ = json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError as error:
        return None, ['not a JSON object: ' + error.msg]
    problems = contracts.validate_response(task, result)
    return (None, problems) if problems else (result, [])


def serialization_hash(tokenizer):
    render_prompt('discovery', '', tokenizer=tokenizer)
    body = {'id': PROTOCOL, 'system': contracts.CHAT_SYSTEM, 'chat_template': tokenizer.chat_template,
            'task_text': contracts.TASK_TEXT, 'field_groups': contracts.FIELD_GROUP,
            'tasks': {name: [value.id, value.kind, list(value.keys)] for name, value in contracts.TASKS.items()}}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
