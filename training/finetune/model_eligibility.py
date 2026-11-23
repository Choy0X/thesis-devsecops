"""Training capability from pinned configuration and weight metadata, without loading a model."""
from __future__ import annotations
import json
import pathlib
import re

_COMMIT = re.compile(r'^[0-9a-f]{40}$')


def check(*, config, files, revision, task=None, tags=(), role=None):
    reason = ''
    architectures = config.get('architectures') or []
    causal = any(str(a).endswith('ForCausalLM') or a in ('GPT2LMHeadModel', 'GPTJForCausalLM', 'GPTNeoForCausalLM') for a in architectures)
    excluded_tags = {'reranker', 'reranking', 'text-ranking', 'feature-extraction', 'sentence-similarity', 'sentence-transformers'}
    excluded_role = str(role or '').lower() in ('embed', 'embedding', 'reranker', 'reranking')
    if excluded_role or excluded_tags.intersection(str(t).lower() for t in tags or ()) or task and task not in ('text-generation', 'conversational'):
        reason = 'Training supports generative causal language models; embedding and reranking models are excluded.'
    elif not causal or config.get('is_encoder_decoder'):
        reason = 'The configuration does not declare a supported generative causal language model.'
    elif config.get('auto_map') and not config.get('model_type'):
        reason = 'Models requiring unverified remote model code are unsupported.'
    elif not any(str(f).endswith('.safetensors') or pathlib.PurePosixPath(str(f)).name.startswith('pytorch_model') and str(f).endswith('.bin') for f in files):
        reason = 'Trainable Hugging Face weights are required; GGUF-only models cannot be fine-tuned.'
    elif not isinstance(revision, str) or not _COMMIT.fullmatch(revision):
        reason = 'A pinnable model repository commit is required.'
    return {'training_eligible': not reason, 'training_reason': reason}


def local(entry):
    root = pathlib.Path(entry.get('path') or '')
    revision = entry.get('main_revision') or next(iter(entry.get('revisions') or []), None)
    if entry.get('source') == 'hub_cache' and revision:
        root = root / 'snapshots' / revision
    try:
        config = json.loads((root / 'config.json').read_text(encoding='utf-8'))
        files = [p.name for p in root.iterdir() if p.is_file()]
    except (OSError, ValueError):
        config, files = {}, []
    task, tags = entry.get('task') or config.get('pipeline_tag'), entry.get('tags') or []
    try:
        import yaml
        with (root / 'README.md').open(encoding='utf-8') as stream:
            card = stream.read(65536)
        if card.startswith('---') and '\n---' in card[3:]:
            metadata = yaml.safe_load(card[3:].split('\n---', 1)[0]) or {}
            if isinstance(metadata, dict):
                task = task or metadata.get('pipeline_tag')
                card_tags = metadata.get('tags') or []
                if isinstance(card_tags, str):
                    card_tags = [card_tags]
                tags = [*tags, *card_tags]
    except (OSError, ValueError, yaml.YAMLError):
        pass
    result = check(config=config, files=files, revision=revision, task=task, tags=tags, role=entry.get('role'))
    if entry.get('source') != 'hub_cache':
        result = {'training_eligible': False, 'training_reason': 'Choose a pinned Hub repository or a cached Hub revision for portable training bundles.'}
    return result


def validate(model_id, revision, *, api=None, config_reader=None):
    from training.finetune import hub
    if api is None:
        # A cached pinned snapshot works offline and retains its Hub identity in portable bundles.
        try:
            from huggingface_hub import snapshot_download
            path = pathlib.Path(snapshot_download(model_id, revision=revision, local_files_only=True))
            config = json.loads((path / 'config.json').read_text(encoding='utf-8'))
            files = [p.name for p in path.iterdir() if p.is_file()]
            result = local({'source': 'hub_cache', 'id': model_id,
                            'path': str(path.parents[1]), 'main_revision': revision})
            if not result['training_eligible']:
                raise ValueError(result['training_reason'])
            return result
        except (OSError, KeyError):
            pass
    client = api or hub._api()
    info = client.model_info(model_id, revision=revision)
    config = getattr(info, 'config', None) or {}
    if not config.get('architectures'):
        if config_reader is None:
            from huggingface_hub import hf_hub_download
            def config_reader(repo, commit):
                return json.loads(pathlib.Path(hf_hub_download(repo, 'config.json', revision=commit)).read_text(encoding='utf-8'))
        config = config_reader(model_id, revision)
    files = [getattr(f, 'rfilename', str(f)) for f in (getattr(info, 'siblings', None) or [])]
    result = check(config=config, files=files, revision=revision, task=getattr(info, 'pipeline_tag', None), tags=getattr(info, 'tags', None))
    if not result['training_eligible']:
        raise ValueError(result['training_reason'])
    return result


def hub_metadata(info):
    result = check(config=getattr(info, 'config', None) or {},
                   files=[getattr(f, 'rfilename', str(f)) for f in (getattr(info, 'siblings', None) or [])],
                   revision=getattr(info, 'sha', None), task=getattr(info, 'pipeline_tag', None), tags=getattr(info, 'tags', None))
    return result
