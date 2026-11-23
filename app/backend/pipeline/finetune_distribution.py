"""Export only validated training imports, preserving their adapter and provenance."""
from __future__ import annotations
from app.backend.pipeline.finetune_deletion import guarded
import json
import pathlib
import re
import zipfile
from app.backend.pipeline import fsio
from training.finetune.notebook_downloads import digest


def import_folder(run_dir, import_id):
    if not isinstance(import_id, str) or not re.fullmatch(r'[0-9a-f]{16}', import_id):
        raise ValueError('Invalid training import ID')
    folder = pathlib.Path(run_dir) / 'imports' / import_id
    record = json.loads((folder / 'import.json').read_text(encoding='utf-8'))
    if record.get('type') != 'trained':
        raise ValueError('Select a validated, finished training import. Smoke runs cannot be exported as trained models.')
    return folder


@guarded
def export(run_dir, import_id, *, format='huggingface', log=print):
    from app.backend.pipeline import oslock
    import_folder(run_dir, import_id)
    root = pathlib.Path(run_dir) / 'exports' / import_id
    root.mkdir(parents=True, exist_ok=True)
    with oslock.FileLock(root / '.export.lock', note=False):
        return _export(run_dir, import_id, format=format, log=log)


def _export(run_dir, import_id, *, format='huggingface', log=print):
    from training.finetune import full_model_export as full
    from app.backend.pipeline import gguf_convert as gc
    if format not in ('huggingface', 'gguf_q4_k_m'):
        raise ValueError('Choose Hugging Face or GGUF Q4_K_M')
    source = import_folder(run_dir, import_id)
    identity = full.source(source)  # Recheck hashes before any loader runs.
    root = pathlib.Path(run_dir) / 'exports' / import_id
    root.mkdir(parents=True, exist_ok=True)
    model = root / 'model'
    if not model.exists():
        full.export(source, model, log=log)
    else:
        manifest = json.loads((model / 'export_manifest.json').read_text(encoding='utf-8'))
        if manifest['source'] != identity:
            raise ValueError('Export source differs from the imported adapter')
        for name, info in manifest['files'].items():
            path = model / name
            if not path.is_file() or digest(path) != info['sha256']:
                raise ValueError('Previously exported model changed: ' + name)
        full.archive_model(model, log=log)
    if format == 'gguf_q4_k_m':
        archive = root / 'gguf-Q4_K_M.zip'
        if not archive.exists():
            entry = {'id': 'trained-' + import_id, 'source': 'folder', 'path': str(model)}
            from app.backend.config import Settings
            from app.backend.pipeline import derived_cache
            record = gc.convert(entry, quant='Q4_K_M', role='chat', log=log,
                                room=derived_cache.for_job(Settings.load().derived_cache_gb))
            directory = gc.CONVERTED / gc.stem(entry, 'f16', 'Q4_K_M')
            with zipfile.ZipFile(archive.with_suffix('.zip.partial'), 'w', zipfile.ZIP_STORED, allowZip64=True) as z:
                for item in record['files']:
                    path = directory / item['name']
                    if digest(path) != item['sha256']: raise ValueError('Converted GGUF changed before archiving')
                    z.write(path, path.name)
                z.write(directory / 'conversion.json', 'conversion.json')
                z.writestr('training_export.json', json.dumps({'schema': 'training-gguf-export/1', 'source': identity,
                    'files': record['files'], 'security_validated': False}, indent=2))
            archive.with_suffix('.zip.partial').replace(archive)
    files = {p.name: {'bytes': p.stat().st_size, 'sha256': digest(p)} for p in root.glob('*.zip')}
    report = {'schema': 'training-distribution/1', 'import_id': import_id, 'source': identity,
              'security_validated': False, 'files': files}
    fsio.write_json(root / 'distribution.json', report)
    return report


def listing(run_dir, import_id):
    import_folder(run_dir, import_id)
    path = pathlib.Path(run_dir) / 'exports' / import_id / 'distribution.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'files': {}}


def artifact(run_dir, import_id, name):
    report = listing(run_dir, import_id)
    if name not in ('hf-model.zip', 'gguf-Q4_K_M.zip') or name not in report['files']:
        raise ValueError('Unknown completed model export')
    path = pathlib.Path(run_dir) / 'exports' / import_id / name
    info = report['files'][name]
    if not path.is_file() or path.stat().st_size != info['bytes'] or digest(path) != info['sha256']:
        raise ValueError('Model archive changed after export')
    return path
