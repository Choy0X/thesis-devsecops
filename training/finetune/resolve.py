"""Resolve the phase contract into one pinned configuration for a run. No torch.

The contract (``training/configs/dataset_finetuning.yaml``) keeps placeholders for the base model commit, the tokenizer
commit, the dataset version and the dataset manifest hash. ``resolve`` fills them: the commits from the hub (only when the
contract does not already pin them), the version and the manifest hash from the dataset folder that was built. The result is
validated in resolved mode (no placeholder survives) and written to ``<run>/resolved_config.json`` together with the hash
of the contract it came from, the pinned commits, the dataset's status (frozen or unfrozen) and a hash of the resolved
config. That file is immutable: resolving a run again with other inputs is refused, and ``read_resolved`` rejects a file
that was edited, or that was resolved against another version of the contract file than the one there now. The contract file itself is never modified.
"""
from __future__ import annotations

import hashlib
import json
import pathlib

import yaml

from training.finetune import config as cfgmod, hub, overrides as ov

SCHEMA = "resolved-config/1"
FILE = "resolved_config.json"


class ResolveError(ValueError):
    """The run cannot be resolved, or its resolved config does not match what was recorded."""


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def resolve(*, dataset_dir, run_dir, config_path=cfgmod.DEFAULT_PATH, api=None, revision: str | None = None,
            tokenizer_revision: str | None = None, overrides: dict | None = None) -> cfgmod.ProjectConfig:
    """The resolved configuration of a run, written to ``run_dir``. ``revision`` and ``tokenizer_revision`` only fill
    placeholders; a commit the contract already pins is kept. ``overrides`` (dotted key to value, see ``overrides.py``) change settings of the
    contract for this run only: they are applied before anything is validated, so the run's ``config_sha256`` covers them, and they are recorded in the file."""
    config_path, dataset_dir, run_dir = pathlib.Path(config_path), pathlib.Path(dataset_dir), pathlib.Path(run_dir)
    data = ov.apply(yaml.safe_load(config_path.read_text(encoding="utf-8")), overrides)
    contract = cfgmod.parse(data, "contract")

    model = data["model"]
    if model["revision"] == cfgmod.REVISION_PLACEHOLDER:
        model["revision"] = revision or hub.resolve_revision(contract.model.id, api=api)
    if model["tokenizer_revision"] == cfgmod.REVISION_PLACEHOLDER:
        model["tokenizer_revision"] = tokenizer_revision or model["revision"]

    manifest_path = dataset_dir / "manifest.json"
    if not manifest_path.exists():
        raise ResolveError(f"{dataset_dir} has no manifest.json: build the dataset first")
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    tag = manifest.get("tag")
    if not isinstance(tag, str) or not tag:
        raise ResolveError(f"{manifest_path} does not name its tag")
    data["data"]["dataset_version"] = tag
    data["data"]["manifest_sha256"] = _sha(manifest_bytes)

    cfg = cfgmod.parse(data, "resolved")
    dumped = cfg.model_dump(mode="json")
    doc = {"schema": SCHEMA, "contract_sha256": cfgmod.file_sha256(config_path),
           "pinned": {"model_revision": cfg.model.revision, "tokenizer_revision": cfg.model.tokenizer_revision},
           "dataset": {"version": tag, "status": manifest.get("dataset_status", "unfrozen"),
                       "manifest_sha256": cfg.data.manifest_sha256, "path": dataset_dir.as_posix()},
           "input_policy_hash": cfg.input_policy_hash(),
           "config": dumped, "config_sha256": _sha(_canonical(dumped).encode("utf-8"))}
    if overrides:
        doc["overrides"] = dict(overrides)             # only when there are some, so a run resolved without any keeps the file it always had
    blob = (json.dumps(doc, sort_keys=True, indent=1) + "\n").encode("utf-8")

    target = run_dir / FILE
    if target.exists():
        if target.read_bytes() != blob:
            raise ResolveError(f"{target} is immutable: this run is already resolved against other inputs; use a new run folder")
        return cfg
    run_dir.mkdir(parents=True, exist_ok=True)
    target.write_bytes(blob)
    return cfg


def read_resolved(run_dir, *, config_path=cfgmod.DEFAULT_PATH, verify_contract: bool = True) -> cfgmod.ProjectConfig:
    """The resolved config of a run, after checking that the file is the one that was written and, unless ``verify_contract`` is False, that it was resolved against the contract file
    that is there now (a run prepared under another version of the contract has to be prepared again; a reader that only shows a run, such as the list of runs, does not verify)."""
    path = pathlib.Path(run_dir) / FILE
    if not path.exists():
        raise ResolveError(f"{path} does not exist: resolve the run first")
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("schema") != SCHEMA:
        raise ResolveError(f"{path} is not a {SCHEMA} file")
    if _sha(_canonical(doc["config"]).encode("utf-8")) != doc.get("config_sha256"):
        raise ResolveError(f"{path} changed after it was written (the config does not match its hash)")
    cfg = cfgmod.parse(doc["config"], "resolved")
    pinned = {"model_revision": cfg.model.revision, "tokenizer_revision": cfg.model.tokenizer_revision}
    if doc.get("pinned") != pinned or doc["dataset"].get("manifest_sha256") != cfg.data.manifest_sha256:
        raise ResolveError(f"{path} changed after it was written (the pinned values do not match the config)")
    if verify_contract:
        recorded = doc.get("contract_sha256")
        try:
            current = cfgmod.file_sha256(config_path)
        except OSError as e:
            raise ResolveError(f"the contract file {config_path} cannot be read, so it cannot be checked that {path} was resolved against it ({e.strerror or e})") from e
        if recorded != current:
            raise ResolveError(f"{path} was resolved against another version of the training contract (recorded {str(recorded)[:12]}, the file now is {current[:12]}): prepare the run again")
    return cfg
