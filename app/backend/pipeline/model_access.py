"""Registry of installed Ollama models and saved Project aliases."""
from __future__ import annotations
import json
import pathlib
import re
from app.backend.pipeline import fsio, oslock
from app.backend.pipeline.ollama import models as ollama_models
from app.backend.pipeline.ollama.client import OllamaError


def installed_models():
    from app.backend.serving_api import serving_models
    grouped = {}
    for row in serving_models()["files"]:
        entry = grouped.setdefault(row['id'], {'id': row['id'], 'name': row['id'], 'source': row['source'],
                                               'role': row.get('role'), 'gguf': [], 'files': []})
        entry['gguf'].append(str(pathlib.Path(row['path']).resolve()))
        entry['files'].append(row)
    return list(grouped.values())


class Registry:
    def __init__(self, root, installed=installed_models):
        self.path = pathlib.Path(root) / 'model_access.json'; self.installed = installed

    def read(self):
        try: return json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError: return {'schema': 'model-access/1', 'clients': {}, 'models': {}}

    def update(self, operation):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with oslock.FileLock(str(self.path) + '.lock', note=False):
            value = self.read(); result = operation(value); fsio.write_json(self.path, value); return result

    def enable(self, model_id, path, *, alias, context=4096, expected_identity=None):
        if not isinstance(alias, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./-]{0,150}', alias) or alias in ('llm', 'embed'):
            raise ValueError('Choose a unique model ID using letters, digits, /, _, - or .')
        if type(context) is not int or not 256 <= context <= 131072: raise ValueError('Context must be 256 to 131072 tokens')
        wanted = pathlib.Path(path).resolve()
        entry = next((m for m in self.installed() if m['id'] == model_id and any(pathlib.Path(p).resolve() == wanted for p in m.get('gguf', []))), None)
        if not entry or not wanted.is_file(): raise ValueError('Choose an installed GGUF file from Model Hub. Convert safetensors models before serving them.')
        identity = fsio.file_identity(path)
        changed = 'The selected model artifact changed during registration. Prepare it again.'
        if identity[0] != str(wanted) or (expected_identity is not None and identity != expected_identity):
            raise ValueError(changed)

        def check_alias(state, name):
            models = state['models']
            previous = models.get(alias)
            reserved = name != alias and any(row['id'] == alias for row in models.values())
            occupied = name in models and models[name]['id'] != name and (
                alias == name or not any(row['id'] == name for row in models.values())
            )
            if reserved or occupied or (previous and previous.get('source_model') != model_id):
                raise ValueError('This API model ID belongs to another installed model. Choose a different ID.')

        expected_name = ollama_models.name_for_variant(wanted, context=context)
        check_alias(self.read(), expected_name)
        from app.backend.pipeline.finetune_deletion import serving_source
        try:
            with serving_source({"model": wanted}):
                name = ollama_models.register_variant(wanted, context=context)
        except OllamaError as error:
            raise OSError(str(error)) from error
        if name != expected_name:
            raise ValueError(changed)
        row = {'id': name, 'ollama_name': name, 'source_model': model_id, 'model': str(wanted), 'context': context,
               'enabled': True, 'embedding': entry.get('role') == 'embedding'}

        def save(state):
            if fsio.file_identity(path) != identity:
                raise ValueError(changed)
            check_alias(state, name)
            # Older gateway clients and saved chats can still address this row by the alias.
            state['models'][alias] = row
        self.update(save)
        return row

    def disable(self, alias):
        def disable_rows(state):
            models = state['models']
            rows = [row for row in models.values() if row['id'] == alias]
            if not rows:
                rows = [models[alias]]
            for row in rows:
                row['enabled'] = False

        self.update(disable_rows)

    def spec(self, alias):
        models = self.read()['models']
        canonical = [row for row in models.values() if row['id'] == alias]
        row = next((item for item in canonical if item['enabled']), None) if canonical else models.get(alias)
        if not row or not row['enabled'] or not pathlib.Path(row['model']).is_file(): return None
        stat = pathlib.Path(row['model']).stat()
        return {'model': row['model'], 'alias': alias, 'context': row['context'], 'embedding': row['embedding'],
                'source_identity': [stat.st_size, stat.st_mtime_ns],
                **({'ollama_name': row['ollama_name']} if 'ollama_name' in row else {})}

    def models(self):
        names = dict.fromkeys(row['id'] for row in self.read()['models'].values() if row['enabled'])
        return [{'id': name, 'object': 'model', 'owned_by': 'local', 'context_length': spec['context']}
                for name in names if (spec := self.spec(name))]


def default():
    from app.backend import paths
    return Registry(paths.interim_dir() / 'inference_api')
