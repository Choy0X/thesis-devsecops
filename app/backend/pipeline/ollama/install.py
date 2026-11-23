"""Install explicitly selected models in the app's local Ollama catalog."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile

from . import catalog, client, models, server

_TAG = re.compile(r"[a-z0-9][a-z0-9_.-]*(?:/[a-z0-9][a-z0-9_.-]*)?(?::[a-z0-9][a-z0-9_.-]*)?")
_REPO = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*")
_SHA = re.compile(r"[0-9a-f]{40}")
_FIELDS = {"pull": {"tag"}, "huggingface": {"repo_id", "revision", "filename", "tag"},
           "gguf": {"path", "tag"}, "adapter": {"run_name", "base_path", "tag"}}


def _relative(value: str) -> bool:
    return bool(value) and "\\" not in value and ":" not in value and all(
        part and part not in (".", "..") for part in value.split("/"))


def validate(params: dict) -> dict:
    kind = params.get("kind")
    fields = _FIELDS.get(kind)
    if fields is None:
        raise ValueError("Choose Ollama pull, Hugging Face GGUF, local GGUF or a training run.")
    if set(params) - fields - {"kind"}:
        raise ValueError("The request has fields for a different install source.")
    data = {"kind": kind, **{key: str(params.get(key, "") or "").strip() for key in fields}}
    tag = data.get("tag", "")
    if (kind == "pull" or tag) and (not _TAG.fullmatch(tag) or catalog.cloud_tag(tag)
                                    or tag.startswith(("aivf/import-", "aivf/runtime-"))):
        raise ValueError("Use a local Ollama name such as qwen2.5-coder:7b-instruct.")
    if kind == "gguf" and (not Path(data["path"]).is_absolute() or Path(data["path"]).suffix.lower() != ".gguf"):
        raise ValueError("Choose a local file with an absolute .gguf path.")
    if kind == "adapter" and (not _relative(data["run_name"]) or not Path(data["base_path"]).is_absolute()):
        raise ValueError("Choose a training run ID and its local GGUF base file.")
    if kind == "huggingface":
        filename = data["filename"]
        if not _REPO.fullmatch(data["repo_id"]) or not _relative(filename) or PurePosixPath(filename).suffix.lower() != ".gguf":
            raise ValueError("Use org/model and its exact .gguf filename.")
        if re.search(r"-\d{5}-of-\d{5}\.gguf$", filename, re.IGNORECASE):
            raise ValueError("This import accepts a complete GGUF file. Choose a single-file variant.")
        data["revision"] = data["revision"] or "main"
    return data


def hub_gguf(repo_id: str, revision: str, filename: str, *, progress=None) -> Path:
    from huggingface_hub import HfApi, hf_hub_download
    from app.backend.pipeline.hub_download import _plain, bytes_progress

    try:
        info = HfApi().model_info(repo_id, revision=revision, files_metadata=progress is not None)
        if not info.sha or not _SHA.fullmatch(info.sha):
            raise ValueError("Hugging Face did not return a pinned commit for this revision.")
        artifact = next((item for item in info.siblings or () if item.rfilename == filename), None)
        if artifact is None:
            raise ValueError("This GGUF filename does not exist at the selected revision.")
        options = {"revision": info.sha}
        if progress is not None:
            size = getattr(artifact, "size", None) or 0
            progress(state="downloading", label=f"Downloading {repo_id}", processed=0, total=size or None, unit="bytes", current=filename)
            options["tqdm_class"] = bytes_progress(progress, size, repo_id, names=(filename,))
        path = Path(hf_hub_download(repo_id, filename, **options))
        if progress is not None:
            size = path.stat().st_size
            progress(state="downloading", label=f"Downloaded {filename}", processed=size, total=size, unit="bytes", current=filename)
        return path
    except Exception as error:
        message = _plain(error, repo_id)
        if message is error:
            raise
        raise message from error


def _published(name: str) -> str:
    row = next((row for row in catalog.installed() if catalog.normalized_tag(row["tag"]) == catalog.normalized_tag(name)), None)
    if not row or not row["available"]:
        raise ValueError(f"Ollama has not reported a runnable model for {name}.")
    if row["location"] != "local":
        raise ValueError("This tag resolves to Ollama Cloud. Local installs require a local model.")
    return row["tag"]


def _merge_for_ollama(run_name: str, base: Path, scratch: Path) -> tuple[Path, str]:
    from app.backend.pipeline import gguf_convert
    from app.backend.pipeline.fsio import file_identity
    from tools import export_reviewer_to_ollama as merge

    run = next((item for item in gguf_convert.run_adapters() if item["run"] == run_name), None)
    if run is None:
        raise ValueError("That managed training run has no adapter here.")
    adapter = Path(run["folder"]).resolve()
    if not adapter.is_relative_to(gguf_convert.RUNS.resolve()):
        raise ValueError("The adapter is outside the managed runs folder.")
    base_info = gguf_convert.base_of(base)
    config_path = adapter.parent / "run_config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if config.get("state") not in ("finished", "early_stopped") or config.get("smoke_test") is not False:
        raise ValueError("Select a finished training run with a real adapter.")
    revision = config.get("model_revision") or ""
    if (not base_info or base_info["id"] != run["base"] or config.get("model") != run["base"]
            or not _SHA.fullmatch(revision) or base_info.get("revision") != revision):
        raise ValueError("Choose a GGUF converted from the training run's exact base model revision.")
    base_entry = gguf_convert.base_entry(run["base"])
    if not base_entry or gguf_convert.model_folder(base_entry).name != revision:
        raise ValueError("Install the original base weights at the training run's revision before converting the adapter.")
    base_identity = file_identity(base)
    weights = adapter / "adapter_model.safetensors"
    weights_identity = file_identity(weights)
    if shutil.disk_usage(scratch).free < base.stat().st_size * 3 + (1 << 30):
        raise ValueError("The local disk does not have enough scratch space to merge and quantize the model.")
    from app.backend.pipeline import finetune_deletion
    with finetune_deletion.serving_source({"adapter": weights}):
        record = gguf_convert.convert_adapter(adapter, base_entry, name="adapter", out_root=scratch)
        if file_identity(weights) != weights_identity:
            raise ValueError("The adapter changed during conversion. Select it again.")
    delta = scratch / "adapter-F16" / record["files"][0]["name"]
    gguf = merge._load_gguf()
    final = scratch / "installed.gguf"
    with merge._temporary_gguf(gguf, base) as original, merge._temporary_gguf(gguf, delta) as patch:
        names = {tensor.name.removesuffix(".lora_a") for tensor in patch.tensors if tensor.name.endswith(".lora_a")}
        metadata = record["adapter"]
        # Targets are written at Q8_0: requantizing them to the base 4-bit types loses most of a LoRA delta.
        merge.merge_adapter(gguf, original, patch, final, expected_targets=names, rank=metadata["rank"], alpha=metadata["alpha"],
                            expected_architecture=record["architecture"])
        try:
            merge.verify_export(gguf, original, patch, final, names, rank=metadata["rank"], alpha=metadata["alpha"])
        except merge.ExportError as error:
            raise ValueError(str(error)) from None
    if file_identity(base) != base_identity:
        raise ValueError("The base changed during the merge. Select it again.")
    slug = re.sub(r"[^a-z0-9]+", "-", run["name"].lower()).strip("-")[:35] or "adapter"
    key = hashlib.sha256(f"{models.name_for(base)}|{metadata['sha256']}".encode()).hexdigest()[:10]
    return final, f"aivf/{slug}-{key}"


def run(params: dict, *, progress) -> str:
    from app.backend import paths
    from app.backend.pipeline.oslock import FileLock

    data = validate(params)
    kind, tag = data["kind"], data["tag"]
    if not client.local_endpoint():
        raise ValueError("Install models only into Ollama running on this computer.")
    server.ensure_serving()
    with FileLock(paths.interim_dir() / "ollama_install.lock", note=False):
        progress(state="installing", label="Preparing Ollama install")
        if kind == "pull":
            if not any(catalog.normalized_tag(row["name"] or "") == catalog.normalized_tag(tag) for row in client.tags()):
                def report(item):
                    progress(state="downloading", label=str(item.get("status") or "Pulling model"),
                             processed=item.get("completed"), total=item.get("total"), unit="bytes")
                client.pull(tag, progress=report)
            installed = _published(tag)
        elif kind == "adapter":
            progress(state="converting", label="Merging trained adapter with its pinned base")
            with tempfile.TemporaryDirectory(prefix="aivf-ollama-merge-") as folder:
                source, generated = _merge_for_ollama(data["run_name"], Path(data["base_path"]).resolve(), Path(folder))
                installed = _published(models.register_named(source, tag or generated, timeout=600.0))
        else:
            if kind == "huggingface":
                source = hub_gguf(data["repo_id"], data["revision"], data["filename"], progress=progress)
            else:
                source = Path(data["path"])
            progress(state="installing", label="Registering GGUF with Ollama")
            filename = PurePosixPath(data["filename"]).name if kind == "huggingface" else source.name
            installed = _published(models.register_named(source, tag, timeout=600.0) if tag else
                                   models.register_gguf(source, context=4096, timeout=600.0, filename=filename))
        progress(state="done", label=f"Installed {installed}", model_tag=installed, processed=1, total=1)
        return installed
