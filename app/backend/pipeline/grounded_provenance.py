"""Input fingerprints of a grounded corpus build: what store, which code, which parsers and tokenizer produced it.

The manifest used to say nothing of its inputs, so two builds from different trees (the working tree is often ahead of HEAD) could not be told apart. This records,
cheaply, enough to answer "was this built from the same inputs": store file size plus collection counts plus a hash of a few kilobytes at both ends of the file (a
3 GB store is not read in full), git HEAD with a dirty flag, a hash of the builder's own source files, parser versions, tokenizer snapshot and training config hashes.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

SOURCE_GLOBS = ('app/backend/pipeline/grounded_*.py', 'tools/build_grounded_corpus.py', 'tools/grounded_review*.py', 'tools/grounded_token_fit.py',
                'training/finetune/security_serialization*.py', 'training/finetune/repair_edits.py', 'training/finetune/grounded_verify.py')
COLLECTIONS = ('advisories', 'fix_commits', 'code_pairs', 'pair_code', 'repositories', 'leakage_groups')
EDGE_BYTES = 65536


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def store_fingerprint(path, reader=None):
    """Size, collection counts and an edge hash of the store file. The edge hash is a change detector, not an integrity proof."""
    path = Path(path)
    size = path.stat().st_size
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        digest.update(handle.read(EDGE_BYTES))
        handle.seek(max(0, size - EDGE_BYTES))
        digest.update(handle.read(EDGE_BYTES))
    out = {'path_name': path.name, 'bytes': size, 'edge_sha256': digest.hexdigest()}
    if reader is not None:
        counts = {}
        for name in COLLECTIONS:
            try:
                counts[name] = reader.count(name)
            except Exception:                                   # ignored: a young store lacks some collections; the count is recorded as None
                counts[name] = None
        out['counts'] = counts
    return out


def source_fingerprint(root):
    """sha256 over the sorted (relative path, file sha256) pairs of the builder's source files."""
    root = Path(root)
    files = sorted({p for pattern in SOURCE_GLOBS for p in root.glob(pattern) if p.is_file()})
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode() + b'\0' + sha256_file(path).encode() + b'\n')
    return {'files': len(files), 'sha256': digest.hexdigest()}


def git_state(root):
    def run(*args):
        try:
            done = subprocess.run(['git', *args], cwd=str(root), capture_output=True, text=True, timeout=60)
            return done.stdout.strip() if done.returncode == 0 else None
        except Exception:                   # ignored: no git or not a repository; the state is recorded as unknown (None)
            return None
    head = run('rev-parse', 'HEAD')
    changed = run('status', '--porcelain', '--untracked-files=no', '--', *[g.rsplit('/', 1)[0] for g in SOURCE_GLOBS if '/' in g])
    return {'head': head, 'builder_files_dirty': None if changed is None else bool(changed), 'builder_files_changed': None if changed is None else len(changed.splitlines())}


def parser_versions():
    from importlib import metadata
    out = {}
    for name in ('tree-sitter', 'tree-sitter-javascript', 'tree-sitter-typescript', 'tree-sitter-python', 'pyarrow', 'transformers', 'tokenizers'):
        try:
            out[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            out[name] = None
    return out


def tokenizer_snapshot(snapshots_dir):
    found = sorted(Path(snapshots_dir).glob('*')) if Path(snapshots_dir).is_dir() else []
    return found[0].name if found else None


def collect(*, root, store, reader=None, quality_source=None, records=None, tokenizer_dir=None, config=None, extra=None):
    """The inputs record that goes into the manifest."""
    root = Path(root)
    out = {'store': store_fingerprint(store, reader), 'git': git_state(root), 'source': source_fingerprint(root), 'parsers': parser_versions(),
           'quality_source': quality_source}
    if records and Path(records).is_file():
        out['records_v29_sha256'] = sha256_file(records)
    if tokenizer_dir:
        out['tokenizer_snapshot'] = tokenizer_snapshot(tokenizer_dir)
    if config and Path(config).is_file():
        out['run_config_sha256'] = sha256_file(config)
    out.update(extra or {})
    return out
