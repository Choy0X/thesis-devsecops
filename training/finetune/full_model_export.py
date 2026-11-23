"""Merge a finished LoRA run into a separate Hugging Face model, without training.

The base is loaded at its pinned revision in BF16 on CPU, never from its quantized
training copy. Exporting a model does not establish its security accuracy.
"""
from __future__ import annotations
import argparse
import json
import os
import pathlib
import re
import shutil
import uuid
import zipfile
from training.finetune.notebook_downloads import digest


def source(run_dir):
    run_dir = pathlib.Path(run_dir)
    manifest = json.loads((run_dir / 'outputs_manifest.json').read_text(encoding='utf-8'))
    for name in ('run_config.json', 'adapter/adapter_config.json', 'adapter/adapter_model.safetensors'):
        expected = manifest.get('files', {}).get(name, {})
        path = run_dir / name
        if not path.is_file() or path.stat().st_size != expected.get('bytes') or digest(path) != expected.get('sha256'):
            raise ValueError('Training output changed or is not manifested: ' + name)
    config = json.loads((run_dir / 'run_config.json').read_text(encoding='utf-8'))
    if config.get('state') not in ('finished', 'early_stopped') or config.get('smoke_test') is not False:
        raise ValueError('A finished real training run is required; smoke or incomplete runs cannot be exported as trained models.')
    for key in ('model_revision', 'tokenizer_revision'):
        if not re.fullmatch(r'[0-9a-f]{40}', config.get(key, '')): raise ValueError('The run must pin ' + key)
    adapter = run_dir / 'adapter'
    adapter_config = json.loads((adapter / 'adapter_config.json').read_text(encoding='utf-8'))
    base = adapter_config.get('base_model_name_or_path')
    if base and base != config['model']: raise ValueError('The adapter names a different base model')
    weights = adapter / 'adapter_model.safetensors'
    if not weights.is_file(): raise ValueError('Adapter weights are missing')
    return {'model': config['model'], 'revision': config['model_revision'], 'tokenizer_revision': config['tokenizer_revision'],
            'adapter_sha256': digest(weights), 'run_config_sha256': digest(run_dir / 'run_config.json'),
            'parameters': (config.get('model_loaded') or {}).get('parameters'), 'adapter': str(adapter.resolve())}


def _merge(model_id, revision, adapter):
    import torch
    from transformers import AutoModelForCausalLM
    from peft import PeftModel
    model = AutoModelForCausalLM.from_pretrained(model_id, revision=revision, trust_remote_code=False,
                                               dtype=torch.bfloat16, device_map={'': 'cpu'})
    return PeftModel.from_pretrained(model, adapter, is_trainable=False).merge_and_unload(safe_merge=True)


def _tokenizer(model_id, revision):
    from transformers import AutoTokenizer
    return AutoTokenizer.from_pretrained(model_id, revision=revision, trust_remote_code=False)


def archive_model(out_dir, *, log=print):
    """Retry distribution after a completed merge without loading the base again."""
    out_dir = pathlib.Path(out_dir)
    manifest = json.loads((out_dir / 'export_manifest.json').read_text(encoding='utf-8'))
    for name, info in manifest['files'].items():
        path = out_dir / name
        if not path.is_file() or path.stat().st_size != info['bytes'] or digest(path) != info['sha256']:
            raise ValueError('Merged model changed before archiving: ' + name)
    archive = out_dir.parent / 'hf-model.zip'
    if archive.exists():
        with zipfile.ZipFile(archive) as zipped:
            if zipped.testzip() or set(zipped.namelist()) != set(manifest['files']) | {'export_manifest.json'}:
                raise ValueError('Previously completed full-model archive is invalid')
        return archive
    partial = archive.with_suffix('.zip.partial')
    with zipfile.ZipFile(partial, 'w', zipfile.ZIP_STORED, allowZip64=True) as zipped:
        for p in sorted(out_dir.rglob('*')):
            if p.is_file():
                log('Archiving ' + p.name); zipped.write(p, p.relative_to(out_dir).as_posix())
    os.replace(partial, archive)
    return archive


def export(run_dir, out_dir, *, merge=None, tokenizer_loader=None, log=print):
    identity = source(run_dir); out_dir = pathlib.Path(out_dir).resolve()
    if out_dir.exists(): raise FileExistsError('The export folder exists. Keep it or choose a new destination.')
    archive = out_dir.parent / 'hf-model.zip'
    if archive.exists(): raise FileExistsError('hf-model.zip already exists; choose a new export directory parent.')
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    if merge is None:
        import psutil
        parameters = identity.get('parameters')
        if parameters:
            required = int(parameters * 2.5 + (2 << 30))
            if psutil.virtual_memory().available < required:
                raise ValueError(f'Full model merge requires approximately {required / (1 << 30):.1f} GiB of free system RAM. Use a high-RAM runtime; the adapter is preserved.')
            if shutil.disk_usage(out_dir.parent).free < parameters * 4 + (2 << 30):
                raise ValueError('Insufficient disk space for the full BF16 model and its ZIP archive.')
    temporary = out_dir.with_name(out_dir.name + '.incomplete-' + uuid.uuid4().hex[:8])
    temporary.mkdir()
    log(f"Merging {identity['model']} at {identity['revision']} on CPU. The original adapter is preserved.")
    model = (merge or _merge)(identity['model'], identity['revision'], identity['adapter'])
    model.save_pretrained(temporary, safe_serialization=True, max_shard_size='2GB')
    tokenizer = (tokenizer_loader or _tokenizer)(identity['model'], identity['tokenizer_revision'])
    tokenizer.save_pretrained(temporary)
    files = {p.relative_to(temporary).as_posix(): {'bytes': p.stat().st_size, 'sha256': digest(p)} for p in sorted(temporary.rglob('*')) if p.is_file()}
    if not any(n.endswith('.safetensors') for n in files): raise ValueError('Merged model weights were not written')
    report = {'schema': 'full-model-export/1', 'format': 'huggingface_merged', 'source': identity, 'files': files,
              'security_validated': False, 'note': 'Includes the merged language model. The auxiliary decision head remains in the original training outputs.'}
    (temporary / 'export_manifest.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, out_dir)
    archive_model(out_dir, log=log)
    log(f'Full model ready: {archive} | SHA256 {digest(archive)}')
    return {**report, 'archive': str(archive), 'path': str(out_dir)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', required=True); parser.add_argument('--out-dir', required=True)
    args = parser.parse_args(argv)
    export(args.run_dir, args.out_dir)


if __name__ == '__main__': main()
