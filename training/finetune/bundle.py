"""The training bundle: everything a GPU runtime needs to run the same code on the same data, and nothing else. No torch.

``bundle.zip`` holds the source package of the fine-tune (``training/``: Python sources, the contract and docs, no notebooks, no app, no
tests), the resolved config, the dataset files (``data/``), ``SOURCE_REVISION`` (the commit and whether the tree was clean) and
``bundle_manifest.json`` with the sha256 and size of every other file. The same inputs give the same bytes (sorted names, fixed
timestamps). ``verify_bundle`` reports files that changed, are missing or are not in the manifest; ``unpack_bundle`` verifies before it
writes anything and refuses paths that would leave the destination. A bundle can be made from an unfrozen dataset, which is recorded as a
warning, and from a dirty source tree, which is flagged in ``SOURCE_REVISION`` and in the manifest.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import subprocess
import zipfile

from training.finetune import resolve as rs

SCHEMA = "training-bundle/1"
MANIFEST = "bundle_manifest.json"
SOURCE_SUFFIXES = (".py", ".yaml", ".md", ".txt", ".json")      # .json: security_serialization_v4 and _v5 read catalogue_v4.json and catalogue_v5.json at import time
_FIXED_TIME = (1980, 1, 1, 0, 0, 0)


class BundleError(RuntimeError):
    """The bundle cannot be made, or does not match its manifest."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git_info(root) -> dict:
    """The commit of the source tree and whether it has uncommitted changes (tracked files only)."""
    try:
        run = lambda *a: subprocess.run(["git", *a], cwd=root, capture_output=True, text=True, timeout=30).stdout.strip()
        commit = run("rev-parse", "HEAD")
        files = [l[3:] for l in run("status", "--porcelain", "--untracked-files=no").splitlines() if l.strip()]
        return {"commit": commit or None, "dirty": bool(files) if commit else None, "dirty_files": sorted(files)[:50]}
    except Exception:
        return {"commit": None, "dirty": None, "dirty_files": []}


def _source_files(root: pathlib.Path) -> dict[str, pathlib.Path]:
    out: dict[str, pathlib.Path] = {}
    base = root / "training"
    for path in sorted(base.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if not path.is_file() or "__pycache__" in rel.split("/") or not rel.endswith(SOURCE_SUFFIXES):
            continue
        if (rel.split("/")[1:2] in (["finetune"], ["configs"])
                or rel in ('training/__init__.py', 'training/requirements-finetune.txt', 'training/requirements-finetune-tpu.txt')):
            out[rel] = path
    return out


def _evaluation_members(dataset_dir) -> tuple[dict[str, bytes], dict]:
    """Package a separately frozen benchmark; training identity remains untouched.

    This checks publication bytes only. Labels, partition roles and overlap with
    training are checked again by evaluation_data.preflight before inference.
    """
    root = pathlib.Path(dataset_dir).resolve()
    manifest_bytes = (root / 'manifest.json').read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest.get('schema') != 'security-benchmark/1' or manifest.get('dataset_status') != 'frozen':
        raise BundleError('The evaluation dataset must be a frozen security-benchmark/1 publication')
    files = manifest.get('files')
    if not isinstance(files, dict) or not files:
        raise BundleError('The evaluation dataset has no hashed publication files')
    members = {'eval_data/manifest.json': manifest_bytes}
    for name, expected in sorted(files.items()):
        relative = pathlib.PurePosixPath(name)
        if (relative.is_absolute() or '..' in relative.parts or '\\' in name or ':' in name
                or not relative.parts or relative.as_posix() != name or name == 'manifest.json'):
            raise BundleError('Unsafe evaluation dataset path: ' + name)
        path = root / name
        if not path.resolve().is_relative_to(root) or path.is_symlink():
            raise BundleError('Linked evaluation dataset path: ' + name)
        body = path.read_bytes()
        if (not isinstance(expected, dict) or _sha(body) != expected.get('sha256')
                or len(body) != expected.get('bytes')):
            raise BundleError('The evaluation dataset file changed: ' + name)
        members['eval_data/' + name] = body
    if not {'records.parquet', 'views.parquet'} <= set(files):
        raise BundleError('The evaluation dataset needs records.parquet and views.parquet')
    return members, {'tag': manifest.get('tag'), 'status': manifest['dataset_status'],
                     'manifest_sha256': _sha(manifest_bytes), 'data_dir': 'eval_data'}


def export_bundle(*, dataset_dir, run_dir, out_path, source_root=None, max_bytes: int | None = None,
                  evaluation_dataset_dir=None) -> dict:
    dataset_dir, run_dir, out_path = pathlib.Path(dataset_dir), pathlib.Path(run_dir), pathlib.Path(out_path)
    root = pathlib.Path(source_root) if source_root else pathlib.Path(__file__).resolve().parents[2]
    cfg = rs.read_resolved(run_dir)                                               # raises when missing, unresolved or edited
    manifest_bytes = (dataset_dir / "manifest.json").read_bytes()
    if _sha(manifest_bytes) != cfg.data.manifest_sha256:
        raise BundleError(f"the manifest of {dataset_dir.name} does not match the resolved config (it changed after the run was resolved)")
    dataset = json.loads(manifest_bytes)

    members: dict[str, bytes] = {}
    for rel, path in _source_files(root).items():
        members[rel] = path.read_bytes()
    members["resolved_config.json"] = (run_dir / rs.FILE).read_bytes()
    for f in sorted(dataset_dir.iterdir()):
        if f.is_file():
            members[f"data/{f.name}"] = f.read_bytes()
    evaluation_identity = None
    if evaluation_dataset_dir is not None:
        evaluation_members, evaluation_identity = _evaluation_members(evaluation_dataset_dir)
        members.update(evaluation_members)
    revision = git_info(root)
    members["SOURCE_REVISION"] = (json.dumps(revision, indent=1, sort_keys=True) + "\n").encode("utf-8")

    warnings = []
    status = dataset.get("dataset_status", "unfrozen")
    if status != "frozen":
        warnings.append({"code": "unfrozen_dataset", "detail": f"the dataset is unfrozen ({dataset.get('tag')})"})
    if revision.get("dirty"):
        warnings.append({"code": "dirty_source_tree", "detail": f"{len(revision['dirty_files'])} tracked file(s) have uncommitted changes"})
    total = sum(len(b) for b in members.values())
    if max_bytes is not None and total > max_bytes:
        raise BundleError(f"the bundle is {total:,} bytes, over the limit of {max_bytes:,}")
    manifest = {"schema": SCHEMA, "files": {n: {"sha256": _sha(b), "bytes": len(b)} for n, b in sorted(members.items())},
                "dataset": {"tag": dataset.get("tag"), "status": status, "manifest_sha256": cfg.data.manifest_sha256},
                "resolved_config_sha256": _sha(members["resolved_config.json"]), "source_revision": revision,
                "warnings": warnings, "total_bytes": total, "data_dir": "data"}
    if evaluation_identity is not None:
        manifest['evaluation_dataset'] = evaluation_identity
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(members):
            info = zipfile.ZipInfo(name, _FIXED_TIME)
            info.compress_type, info.external_attr = zipfile.ZIP_DEFLATED, 0o644 << 16
            z.writestr(info, members[name])
        info = zipfile.ZipInfo(MANIFEST, _FIXED_TIME)
        info.compress_type, info.external_attr = zipfile.ZIP_DEFLATED, 0o644 << 16
        z.writestr(info, (json.dumps(manifest, indent=1, sort_keys=True) + "\n").encode("utf-8"))
    return {"path": str(out_path), "files": len(members), "bytes": total, "warnings": warnings,
            "sha256": _sha(out_path.read_bytes())}


def verify_bundle(path) -> list[str]:
    """What is wrong with a bundle: files that changed, are missing, or are not in its manifest."""
    with zipfile.ZipFile(path) as z:
        if MANIFEST not in z.namelist():
            return [f"{MANIFEST} is missing"]
        manifest = json.loads(z.read(MANIFEST))
        listed, present = manifest["files"], set(z.namelist()) - {MANIFEST}
        problems = []
        for name, info in sorted(listed.items()):
            if name not in present:
                problems.append(f"{name} is missing from the bundle")
            elif _sha(z.read(name)) != info["sha256"]:
                problems.append(f"{name} changed after the bundle was made")
        problems += [f"{name} is in the bundle but not in the manifest" for name in sorted(present - set(listed))]
    return problems


def unpack_bundle(path, dest) -> dict:
    """Verify, then write the files under ``dest``; nothing is written when anything is wrong."""
    dest = pathlib.Path(dest)
    problems = verify_bundle(path)
    with zipfile.ZipFile(path) as z:
        manifest = json.loads(z.read(MANIFEST)) if MANIFEST in z.namelist() else {"files": {}}
        for name in list(manifest["files"]) + z.namelist():
            parts = pathlib.PurePosixPath(name).parts
            if name.startswith("/") or ".." in parts or (parts and ":" in parts[0]):
                raise BundleError(f"the bundle holds a path that would leave the destination: {name}")
        if problems:
            raise BundleError("; ".join(problems))
        for name in z.namelist():
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(name))
    return manifest
