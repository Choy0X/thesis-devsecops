"""Verified run5 performance overlay. Original bundle/config files stay intact."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import sys


def _read(path, maximum=1 << 20):
    if path.stat().st_size > maximum:
        raise ValueError('Overlay metadata exceeds its size bound: ' + str(path))
    return path.read_bytes()


def _sha(body):
    return hashlib.sha256(body).hexdigest()


def verify_overlay(patch_dir, bundle_dir, run_dir):
    patch_dir, bundle_dir, run_dir = map(Path, (patch_dir, bundle_dir, run_dir))
    body = _read(patch_dir / 'patch_manifest.json')
    manifest = json.loads(body)
    receipt = json.loads(_read(patch_dir / 'patch_receipt.json'))
    if (manifest.get('schema') != 'a100-runtime-overlay/1'
            or receipt.get('manifest_sha256') != _sha(body)
            or manifest.get('base_bundle_manifest_sha256') != _sha(_read(bundle_dir / 'bundle_manifest.json'))):
        raise ValueError('Performance overlay does not match the verified run5 bundle')
    expected = {'trainer.py', 'throughput.py', 'launch.py'}
    if set(manifest.get('files', {})) != expected:
        raise ValueError('Unexpected performance overlay source inventory')
    for name, identity in manifest['files'].items():
        source = _read(patch_dir / name, 256 << 10)
        if identity != {'sha256': _sha(source), 'bytes': len(source)}:
            raise ValueError('Performance overlay source changed: ' + name)
    sources = manifest.get('base_sources', {})
    if set(sources) != {'training/finetune/trainer.py', 'training/finetune/throughput.py'}:
        raise ValueError('Unexpected original source inventory')
    for name, expected_sha in sources.items():
        if _sha(_read(bundle_dir / name, 256 << 10)) != expected_sha:
            raise ValueError('Original bundle source changed: ' + name)
    if _read(run_dir / 'resolved_config.json') != _read(bundle_dir / 'resolved_config.json'):
        raise ValueError('Saved run configuration differs from the original bundle')
    return manifest


def load_overlay(patch_dir, bundle_dir, manifest):
    patch_dir, bundle_dir = Path(patch_dir).resolve(), Path(bundle_dir).resolve()
    sys.path.insert(0, str(bundle_dir))
    parent = importlib.import_module('training.finetune')
    for name in ('trainer', 'throughput'):
        canonical = 'training.finetune.' + name
        spec = importlib.util.spec_from_file_location(canonical, patch_dir / (name + '.py'))
        module = importlib.util.module_from_spec(spec)
        sys.modules[canonical] = module
        spec.loader.exec_module(module)
        setattr(parent, name, module)
    parent.throughput.RUNTIME_PATCH = {
        'schema': manifest['schema'], 'id': manifest['id'], 'files': manifest['files'],
        'base_bundle_sha256': manifest.get('base_bundle_sha256'),
        'manifest_sha256': _sha(_read(patch_dir / 'patch_manifest.json'))}
    runner = importlib.import_module('training.finetune.runner')
    if runner.tr is not parent.trainer:
        raise RuntimeError('Runner did not bind the verified adaptive trainer')
    return parent


def main(argv=None):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--verify-only', action='store_true')
    parser.add_argument('--check-imports', action='store_true')
    own, arguments = parser.parse_known_args(argv)
    def argument(name):
        if name not in arguments or arguments.index(name) + 1 >= len(arguments):
            raise ValueError('Overlay launch requires ' + name)
        return Path(arguments[arguments.index(name) + 1]).resolve()
    bundle, run = argument('--bundle'), argument('--run-dir')
    patch = Path(__file__).resolve().parent
    manifest = verify_overlay(patch, bundle, run)
    if not own.verify_only:
        load_overlay(patch, bundle, manifest)
    if own.verify_only or own.check_imports:
        print(json.dumps({'patch': manifest['id'], 'verified': True,
                          'imports_checked': own.check_imports,
                          'model_weights_loaded': False, 'training_started': False}), flush=True)
        return 0
    print('Verified adaptive A100 overlay: ' + manifest['id'] + '; resuming original run and checkpoint.', flush=True)
    from training.finetune import runner
    return runner.main(arguments)


if __name__ == '__main__':
    raise SystemExit(main())
