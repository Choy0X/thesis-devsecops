"""Authenticate an experimental trial adapter without loading model weights.

Hashes establish consistency with the supplied original bundle, not who ran the
experiment. Source trials remain experimental and never become full training
results merely because their adapter can be evaluated.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import zipfile

MAX_FILES = 4096
MAX_BYTES = 16 << 30
MAX_MEMBER_BYTES = 8 << 30
MAX_JSON_BYTES = 16 << 20
MANIFEST = 'trial_outputs_manifest.json'
ADAPTER = 'adapter/adapter_model.safetensors'
ADAPTER_CONFIG = 'adapter/adapter_config.json'


class TrialAdapterError(ValueError):
    """The artifact cannot establish the required trial-adapter identity."""


def _safe(name):
    if not isinstance(name, str) or not name or len(name) > 1024:
        return False
    path = PurePosixPath(name)
    reserved = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}
    return (not path.is_absolute() and path.as_posix() == name and '\\' not in name
            and all(part not in ('.', '..') and ':' not in part and not part.endswith(('.', ' '))
                    and part.split('.')[0].upper() not in reserved
                    and not any(ord(char) < 32 for char in part) for part in path.parts))


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TrialAdapterError('Duplicate JSON key: ' + key)
        result[key] = value
    return result


def _json(raw):
    try:
        value = json.loads(raw, object_pairs_hook=_pairs,
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite JSON')))
    except (ValueError, UnicodeError, TypeError) as error:
        raise TrialAdapterError(f'Invalid JSON object: {error}') from error
    if not isinstance(value, dict):
        raise TrialAdapterError('Expected a JSON object')
    return value


class _Inventory:
    def __init__(self, source):
        self.path = Path(source).resolve()
        self.archive = None
        self.members = {}

    def __enter__(self):
        try:
            if self.path.is_dir():
                for path in self.path.rglob('*'):
                    attrs = path.lstat()
                    if (path.is_symlink() or getattr(attrs, 'st_file_attributes', 0) & 0x400
                            or not path.resolve().is_relative_to(self.path)):
                        raise TrialAdapterError('Linked artifact path is forbidden')
                    if path.is_dir():
                        continue
                    if not path.is_file():
                        raise TrialAdapterError('Only regular artifact files are supported')
                    self.members[path.relative_to(self.path).as_posix()] = attrs.st_size
            else:
                self.archive = zipfile.ZipFile(self.path)
                for entry in self.archive.infolist():
                    if (entry.filename in self.members or entry.is_dir() or entry.flag_bits & 1
                            or stat.S_ISLNK(entry.external_attr >> 16)):
                        raise TrialAdapterError('Unsafe or duplicate artifact inventory')
                    self.members[entry.filename] = entry.file_size
            names = list(self.members)
            if (not names or len(names) > MAX_FILES or len(set(name.casefold() for name in names)) != len(names)
                    or any(not _safe(name) for name in names)
                    or any(size < 0 or size > MAX_MEMBER_BYTES for size in self.members.values())
                    or sum(self.members.values()) > MAX_BYTES):
                raise TrialAdapterError('Unsafe paths or artifact inventory exceeds bounds')
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        if self.archive:
            self.archive.close()

    @contextmanager
    def open(self, name):
        if not _safe(name) or name not in self.members:
            raise TrialAdapterError('Missing or unsafe artifact member: ' + str(name))
        if self.archive:
            with self.archive.open(name) as stream:
                yield stream
        else:
            path = self.path / name
            if path.is_symlink() or not path.resolve().is_relative_to(self.path):
                raise TrialAdapterError('Linked artifact member: ' + name)
            with path.open('rb') as stream:
                yield stream

    def read(self, name):
        if self.members.get(name, MAX_JSON_BYTES + 1) > MAX_JSON_BYTES:
            raise TrialAdapterError('Missing or over-budget JSON artifact: ' + name)
        with self.open(name) as stream:
            raw = stream.read(MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES:
            raise TrialAdapterError('Over-budget JSON artifact: ' + name)
        return raw

    def authenticate(self, manifest_name, schema, *, extras=()):
        raw = self.read(manifest_name)
        manifest = _json(raw)
        files = manifest.get('files')
        if manifest.get('schema') != schema or not isinstance(files, dict):
            raise TrialAdapterError('Artifact manifest schema or files mapping differs')
        if set(files) != set(self.members) - {manifest_name} - set(extras):
            raise TrialAdapterError('Artifact manifest inventory differs')
        for name, expected in files.items():
            if (not isinstance(expected, dict) or type(expected.get('bytes')) is not int
                    or expected['bytes'] != self.members[name]
                    or not re.fullmatch('[0-9a-f]{64}', str(expected.get('sha256')))):
                raise TrialAdapterError('Artifact member identity differs: ' + name)
            digest, count = hashlib.sha256(), 0
            with self.open(name) as stream:
                for block in iter(lambda: stream.read(1 << 20), b''):
                    count += len(block)
                    if count > expected['bytes']:
                        raise TrialAdapterError('Artifact member identity differs: ' + name)
                    digest.update(block)
            if count != expected['bytes'] or digest.hexdigest() != expected['sha256']:
                raise TrialAdapterError('Artifact member identity differs: ' + name)
        return manifest, hashlib.sha256(raw).hexdigest()


def _bundle_source(bundle_dir):
    path = Path(bundle_dir)
    return path / 'bundle.zip' if path.is_dir() and not (path / 'bundle_manifest.json').exists() else path


def _tensor_header(source, cfg):
    with source.open(ADAPTER) as stream:
        prefix = stream.read(8)
        length = int.from_bytes(prefix, 'little')
        if len(prefix) != 8 or not 2 <= length <= min(MAX_JSON_BYTES, source.members[ADAPTER] - 8):
            raise TrialAdapterError('Invalid safetensors header length')
        header = _json(stream.read(length))
    tensors = {key: value for key, value in header.items() if key != '__metadata__'}
    ranges, pairs, targets = [], {}, set()
    sizes = {'F64': 8, 'F32': 4, 'F16': 2, 'BF16': 2}
    try:
        for key, value in tensors.items():
            match = re.fullmatch(r'(.+\.([^.]+))\.lora_([AB])(?:\.default)?\.weight', key)
            if not match or match[2] not in cfg.lora.target_modules:
                raise ValueError('non-LoRA or undeclared projection')
            shape, offsets = value['shape'], value['data_offsets']
            if (not isinstance(shape, list) or len(shape) != 2 or any(type(n) is not int or n <= 0 for n in shape)
                    or shape[0 if match[3] == 'A' else 1] != cfg.lora.r
                    or not isinstance(offsets, list) or len(offsets) != 2
                    or any(type(n) is not int for n in offsets) or offsets[0] < 0
                    or offsets[1] - offsets[0] != math.prod(shape) * sizes[value['dtype']]):
                raise ValueError('invalid rank, shape, dtype or byte offsets')
            parts = pairs.setdefault(match[1], set())
            if match[3] in parts:
                raise ValueError('duplicate adapter factor')
            parts.add(match[3]); targets.add(match[2]); ranges.append(tuple(offsets))
        ranges.sort()
        if (not ranges or ranges[0][0] != 0 or ranges[-1][1] != source.members[ADAPTER] - 8 - length
                or any(a[1] != b[0] for a, b in zip(ranges, ranges[1:]))
                or any(parts != {'A', 'B'} for parts in pairs.values())
                or targets != set(cfg.lora.target_modules)):
            raise ValueError('missing factors, projections or incomplete contiguous payload')
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError) as error:
        raise TrialAdapterError(f'Invalid LoRA safetensors layout: {error}') from error
    return {'tensors': len(tensors), 'factor_pairs': len(pairs), 'header_bytes': length,
            'payload_bytes': source.members[ADAPTER] - 8 - length, 'weights_loaded': False,
            'architecture_completeness': 'not_checked_until_attachment',
            'tensor_layout': {name: value['shape'] for name, value in tensors.items()}}


def _verify(source, original, *, allow_software):
    from training.finetune import config as config_module, runtime_compatibility
    bundle, bundle_sha = original.authenticate('bundle_manifest.json', 'training-bundle/1')
    manifest, output_sha = source.authenticate(MANIFEST, 'trial-outputs/1', extras=('import.json',))
    required = {'trial_config.json', 'resolved_config.json', 'effective_config.json', ADAPTER, ADAPTER_CONFIG}
    if not required <= set(manifest['files']):
        raise TrialAdapterError('Trial adapter/configuration is missing, including possible compact recovery')
    resolved_raw = original.read('resolved_config.json')
    resolved = _json(resolved_raw)
    if (resolved.get('schema') != 'resolved-config/1' or source.read('resolved_config.json') != resolved_raw
            or hashlib.sha256(json.dumps(resolved['config'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                != resolved.get('config_sha256')):
        raise TrialAdapterError('Original resolved configuration identity differs')
    cfg = config_module.parse(resolved['config'], 'resolved')
    raw = source.read('trial_config.json'); trial = _json(raw)
    software = trial.get('software_test') is True
    if (trial.get('schema') != 'source-trial/1' or trial.get('mode') != 'trial'
            or trial.get('state') not in ('finished', 'partial') or trial.get('experimental') is not True
            or any(trial.get(key) is not False for key in ('counts_as_full_training', 'counts_as_evaluation', 'security_evaluation'))
            or type(trial.get('software_test')) is not bool):
        raise TrialAdapterError('Expected an explicitly experimental source-trial/1 adapter')
    if (software or trial.get('software_int4')) and not allow_software:
        raise TrialAdapterError('A software trial requires explicit allow_software')
    expected = {'model': cfg.model.id, 'model_revision': cfg.model.revision,
                'tokenizer_revision': cfg.model.tokenizer_revision, 'serialization': cfg.model.serialization,
                'resolved_config_sha256': hashlib.sha256(resolved_raw).hexdigest()}
    if any(trial.get(key) != value for key, value in expected.items()):
        raise TrialAdapterError('Trial model/tokenizer/protocol/config identity differs')
    data_raw = original.read('data/manifest.json'); data = _json(data_raw)
    data_sha = hashlib.sha256(data_raw).hexdigest()
    if (not isinstance(trial.get('bundle'), dict) or trial['bundle'].get('manifest_sha256') != bundle_sha
            or trial.get('dataset') != {'version': data.get('tag'), 'manifest_sha256': data_sha}
            or cfg.data.manifest_sha256 != data_sha or cfg.data.dataset_version != data.get('tag')
            or resolved.get('pinned') != {'model_revision': cfg.model.revision, 'tokenizer_revision': cfg.model.tokenizer_revision}
            or resolved.get('dataset', {}).get('manifest_sha256') != data_sha):
        raise TrialAdapterError('Trial bundle or training dataset identity differs')
    steps, adapter = trial.get('steps_done'), trial.get('adapter')
    if (type(steps) is not int or steps <= 0 or not isinstance(adapter, dict)
            or type(adapter.get('steps_done')) is not int or adapter['steps_done'] != steps
            or adapter.get('sha256') != manifest['files'][ADAPTER]['sha256']):
        raise TrialAdapterError('Trial adapter identity or completed optimizer steps are inconsistent')
    stop = trial.get('resource_stop')
    if stop is not None and (not isinstance(stop, dict) or type(stop.get('consistent_step')) is not int
                             or stop['consistent_step'] != steps or stop.get('optimizer_updated_in_failed_attempt') is True):
        raise TrialAdapterError('Partial resource-stop steps differ from the saved consistent adapter')
    recipe = trial.get('runtime_recipe')
    try:
        effective = runtime_compatibility.validate_recipe(cfg, recipe)
    except (ValueError, TypeError, KeyError) as error:
        raise TrialAdapterError(f'Trial runtime recipe differs: {error}') from error
    if recipe['backend'] == 'cpu' and not software:
        raise TrialAdapterError('CPU recipe requires a software trial')
    if _json(source.read('effective_config.json')) != effective.model_dump(mode='json'):
        raise TrialAdapterError('Trial effective configuration differs from its runtime recipe')
    ac = _json(source.read(ADAPTER_CONFIG))
    if (ac.get('peft_type') != 'LORA' or ac.get('base_model_name_or_path') not in
            ((cfg.model.id, '', None) if software and allow_software else (cfg.model.id,))
            or ac.get('revision') not in (None, cfg.model.revision)):
        raise TrialAdapterError('LoRA adapter base model identity differs')
    for key in ('r', 'lora_alpha', 'lora_dropout', 'bias', 'task_type', 'use_rslora', 'use_dora'):
        if ac.get(key, False if key in ('use_rslora', 'use_dora') else None) != getattr(cfg.lora, key):
            raise TrialAdapterError('LoRA adapter configuration differs: ' + key)
    if set(ac.get('target_modules') or []) != set(cfg.lora.target_modules) or ac.get('rank_pattern') or ac.get('alpha_pattern'):
        raise TrialAdapterError('LoRA target modules or per-module overrides differ')
    if any(ac.get(key) for key in ('modules_to_save', 'target_parameters', 'trainable_token_indices',
                                   'layer_replication', 'layers_to_transform', 'alora_invocation_tokens',
                                   'lora_bias', 'use_qalora', 'use_bdlora', 'ensure_weight_tying')):
        raise TrialAdapterError('LoRA configuration enables unrecorded parameter changes')
    layout = _tensor_header(source, cfg)
    return {'schema': 'verified-trial-adapter/1', 'artifact_kind': 'source_trial', **expected,
            'state': trial['state'], 'steps_done': steps, 'experimental': True,
            'counts_as_full_training': False, 'counts_as_evaluation': False, 'security_evaluation': False,
            'software_test': software, 'smoke_test': software, 'adapter_sha256': adapter['sha256'],
            'adapter_config_sha256': manifest['files'][ADAPTER_CONFIG]['sha256'],
            'trial_config_sha256': hashlib.sha256(raw).hexdigest(), 'outputs_manifest_sha256': output_sha,
            'bundle': trial['bundle'], 'training_dataset': trial['dataset'], 'runtime_recipe': recipe,
            'serialization_members': {name: bundle['files'][name]['sha256'] for name in
                ('training/finetune/serialization.py', 'training/finetune/sft_serialization.py',
                 'training/finetune/contracts.py', 'training/finetune/sft_contracts.py') if name in bundle['files']},
            'resolved_config': resolved, 'config': cfg.model_dump(mode='json'),
            'effective_config': effective.model_dump(mode='json'), 'safetensors': layout}


def verify_trial_adapter(source, *, bundle_dir, allow_software=False):
    """Verify an imported folder or trial ZIP against its original bundle."""
    try:
        with _Inventory(source) as trial, _Inventory(_bundle_source(bundle_dir)) as original:
            return _verify(trial, original, allow_software=allow_software)
    except (OSError, KeyError, TypeError, zipfile.BadZipFile, RuntimeError) as error:
        raise TrialAdapterError(f'Trial adapter verification failed: {error}') from error


def materialize_trial_adapter(source, *, bundle_dir, out_dir, allow_software=False):
    """Authenticate and copy only config and inert adapter bytes to a fresh tree."""
    metadata = verify_trial_adapter(source, bundle_dir=bundle_dir, allow_software=allow_software)
    out = Path(out_dir).resolve()
    if out.exists():
        raise TrialAdapterError('Trial adapter materialization requires a fresh output directory')
    out.mkdir(parents=True)
    try:
        (out / 'adapter').mkdir()
        with _Inventory(source) as inventory:
            for name, expected in ((ADAPTER, metadata['adapter_sha256']), (ADAPTER_CONFIG, metadata['adapter_config_sha256'])):
                digest, count = hashlib.sha256(), 0
                with inventory.open(name) as stream, (out / name).open('wb') as target:
                    for block in iter(lambda: stream.read(1 << 20), b''):
                        count += len(block)
                        if count > inventory.members[name]:
                            raise TrialAdapterError('Adapter size changed during materialization')
                        digest.update(block); target.write(block)
                if digest.hexdigest() != expected:
                    raise TrialAdapterError('Adapter identity changed during materialization')
    except Exception:
        shutil.rmtree(out)
        raise
    return {**metadata, 'adapter_dir': str(out / 'adapter')}
