"""Export the accepted v40 stage A LoRA and local Qwen2.5-Coder base to Ollama.

The conversion uses an isolated scratch directory. The LoRA delta is added to the
dequantized Q4_K_M base and every target tensor is written at Q8_0: requantizing
to the base 4-bit types loses almost all of the delta (about 1/25 of a 4-bit step).
The output directory is published only after the final GGUF has been checked
against the base: untouched tensors byte for byte, every target at Q8_0 with at
least MIN_RETENTION of the delta kept, and the embedded chat template identical
to the pinned training tokenizer config.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import importlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
import uuid

import numpy as np

from app.backend.pipeline import gguf_convert, oslock
from app.backend.pipeline.fsio import file_identity
from app.backend.pipeline.ollama import client, server


DEFAULT_OUTPUT = gguf_convert.TOOLS.parent.parent / "models" / "aivf-reviewer-v40-q8"
MODEL_FILE = "reviewer-v40-q8_0-targets.gguf"
# Share of the LoRA delta (projection of final - base onto the delta) every target must keep.
MIN_RETENTION = 0.8
RECEIPT_FILE = "export.json"
CONVERTER_TAG = "b11528"
ADAPTER_WEIGHTS_SHA = "1bee800ac0d3aee0281eb2905e1b2ba64c1b409a34db533f08a1ac15c4c22e46"
ADAPTER_CONFIG_SHA = "c4a1bd3377dd513d339ea3210a4f97330a3458edc860533e087c02d5a83e6099"
ADAPTER_CARD_SHA = "0075cf5923ce34cb5b146f95c10f8657e07e3ef2e74b3a596ebc13828f0e80c5"
CONVERTED_ADAPTER_SHA = "d795ab3d1ad80a2c865c8ef151aab4d1e080ff48284fd05bed39a1e7aa53601c"
BASE_GGUF_SHA = "60e05f2100071479f596b964f89f510f057ce397ea22f2833a0cfe029bfc2463"
BASE_CONFIG_SHA = {
    "config.json": "c0242402ad6a13b331ea320feea8c7e3776ffb7a4eff0757b9cd667e116d9a28",
    "generation_config.json": "1a628a5775bc69cde01c6749a531150ca4d3189652c618a174f7077923acf3b1",
    "tokenizer_config.json": "959e7f1d9a1b7641a6d6ce05ca97b75c7894fcb66cbe5a040406458fb1128ee4",
}
TARGET_MODULES = ("attn_q", "attn_k", "attn_v", "attn_output", "ffn_gate", "ffn_up", "ffn_down")
STAGE_TIMEOUT = 60 * 60
DISK_MARGIN = 64 * 1024 ** 2
COMPARE_CHUNK = 8 * 1024 ** 2


class ExportError(RuntimeError):
    """An offline input, export stage, or local import could not be verified."""


def modelfile(merged_gguf: Path) -> str:
    path = merged_gguf.as_posix()
    if not merged_gguf.is_absolute():
        path = "./" + path
    return f'FROM "{path}"\nPARAMETER num_ctx 4096\nPARAMETER temperature 0\n'


def convert_command(adapter: Path, base_config: Path, out: Path) -> list[str]:
    return [sys.executable, "-B", "-s", str(gguf_convert.TOOLS / gguf_convert.LORA_SCRIPT),
            str(adapter), "--base", str(base_config), "--outfile", str(out), "--outtype", "f16"]


def _require_sha256(path: Path, expected: str) -> None:
    if not path.is_file():
        raise ExportError(f"Required local file is missing: {path}")
    if gguf_convert._sha256(path) != expected:
        raise ExportError(f"{path} does not match its accepted SHA-256 ({expected}).")


def _load_gguf():
    tools = gguf_convert.TOOLS
    record = gguf_convert._record(tools) or {}
    if record.get("tag") != CONVERTER_TAG:
        raise ExportError(f"The export requires the installed llama.cpp converter {CONVERTER_TAG} in {tools}.")
    differences = gguf_convert.verify(tools)
    if differences:
        raise ExportError("The installed converter differs from its record: " + ", ".join(differences[:4]))
    vendor = str(tools / "gguf-py")
    old_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    sys.path.insert(0, vendor)
    try:
        gguf = importlib.import_module("gguf")
    finally:
        sys.path.remove(vendor)
        sys.dont_write_bytecode = old_bytecode
    if Path(gguf.__file__).resolve() != (Path(vendor) / "gguf" / "__init__.py").resolve():
        raise ExportError("The loaded GGUF library is outside the verified converter installation.")
    return gguf


def _field(reader, name: str):
    field = reader.fields.get(name)
    return field.contents() if field is not None else None


def _tensor_map(reader):
    tensors = {tensor.name: tensor for tensor in reader.tensors}
    if len(tensors) != len(reader.tensors):
        raise ExportError("A GGUF input contains duplicate tensor names.")
    return tensors


@contextmanager
def _temporary_gguf(gguf, path: Path):
    # GGUFReader has no close API. Retain the instance even if parsing fails so
    # its NumPy mapping cannot keep a temporary file open on Windows.
    reader = object.__new__(gguf.GGUFReader)
    try:
        reader.__init__(path)
        yield reader
    finally:
        if hasattr(reader, "data"):
            reader.data._mmap.close()


def merge_adapter(gguf, base, adapter, output: Path, *, expected_targets: set[str], rank: int, alpha: float,
                  expected_architecture: str = "qwen2") -> set[str]:
    """Write the final GGUF: Q8_0 merged LoRA targets, every other tensor copied from the base."""
    architecture = _field(base, "general.architecture")
    if architecture != expected_architecture or _field(adapter, "general.architecture") != architecture:
        raise ExportError(f"The base and adapter must both have the {expected_architecture} architecture.")
    if base.endianess != gguf.GGUFEndian.LITTLE or adapter.endianess != base.endianess:
        raise ExportError("The base and adapter must both be little-endian GGUF files.")
    if _field(adapter, "general.type") != "adapter" or _field(adapter, "adapter.type") != "lora":
        raise ExportError("The GGUF must contain a LoRA adapter.")
    if rank <= 0 or not np.isfinite(alpha) or _field(adapter, "adapter.lora.alpha") != alpha:
        raise ExportError("The LoRA scaling does not match the accepted adapter.")

    base_tensors = _tensor_map(base)
    adapter_tensors = _tensor_map(adapter)
    expected_members = {target + suffix for target in expected_targets for suffix in (".lora_a", ".lora_b")}
    extra = adapter_tensors.keys() - expected_members
    missing = expected_members - adapter_tensors.keys()
    if extra:
        raise ExportError(f"LoRA contains an extra tensor: {min(extra)}.")
    if missing or not expected_members:
        raise ExportError("LoRA contains a missing pair" + (f" member: {min(missing)}." if missing else "."))

    pairs = {}
    for name in sorted(expected_targets):
        tensor = base_tensors.get(name)
        if tensor is None:
            raise ExportError(f"The LoRA target {name} is missing in the base GGUF.")
        a, b = (adapter_tensors[name + suffix] for suffix in (".lora_a", ".lora_b"))
        if a.tensor_type != gguf.GGMLQuantizationType.F16 or b.tensor_type != gguf.GGMLQuantizationType.F16:
            raise ExportError(f"The LoRA pair for {name} must contain F16 weights.")
        shape = tuple(reversed(tensor.shape))
        if len(shape) != 2 or a.data.shape != (rank, shape[1]) or b.data.shape != (shape[0], rank):
            raise ExportError(f"The LoRA pair for {name} has the wrong shape for the base matrix.")
        if shape[1] % gguf.GGML_QUANT_SIZES[_target_type(gguf)][0]:
            raise ExportError(f"The LoRA target {name} cannot be stored as Q8_0 (row length {shape[1]}).")
        pairs[name] = (a.data, b.data)

    q8_0 = _target_type(gguf)
    writer = gguf.GGUFWriter(output, arch=architecture, endianess=base.endianess)
    try:
        for name, field in base.fields.items():
            if name.startswith("GGUF.") or name in {"general.architecture", "general.alignment"}:
                continue
            kind = field.types[0]
            subtype = field.types[-1] if kind == gguf.GGUFValueType.ARRAY else None
            writer.add_key_value(name, field.contents(), kind, subtype)
        if "general.alignment" in base.fields:
            writer.add_custom_alignment(base.alignment)

        for tensor in base.tensors:
            if tensor.name in pairs:
                rows, columns = reversed(tensor.shape)
                block, size = gguf.GGML_QUANT_SIZES[q8_0]
                row_bytes = int(columns) // block * size
                writer.add_tensor_info(tensor.name, (int(rows), row_bytes), np.dtype(np.uint8), int(rows) * row_bytes, q8_0)
            else:
                writer.add_tensor_info(tensor.name, tensor.data.shape, tensor.data.dtype, int(tensor.n_bytes), tensor.tensor_type)

        writer.write_header_to_file()
        writer.write_kv_data_to_file()
        writer.write_ti_data_to_file()
        for index, tensor in enumerate(base.tensors, start=1):
            if tensor.name in pairs:
                a, b = pairs[tensor.name]
                delta = b.astype(np.float32) @ a.astype(np.float32)
                delta *= alpha / rank
                delta += gguf.dequantize(tensor.data, tensor.tensor_type)
                if not np.isfinite(delta).all():
                    raise ExportError(f"Merging {tensor.name} produced non-finite weights.")
                merged = gguf.quantize(delta, q8_0)
                writer.write_tensor_data(merged)
                del delta, merged
            else:
                writer.write_tensor_data(tensor.data, tensor_endianess=base.endianess)
            if index % 20 == 0:
                print(f"Merged or copied {index}/{len(base.tensors)} tensors.", flush=True)
    finally:
        writer.close()
    return set(pairs)


def _target_type(gguf):
    """The tensor type every merged LoRA target is stored with."""
    return gguf.GGMLQuantizationType.Q8_0


def retention(gguf, base_tensor, final_tensor, a, b, scale: float) -> tuple[float, float]:
    """Projection of (final - base) onto the LoRA delta and their cosine, without forming the delta.

    With D = scale * B @ A: <X, D> = scale * sum((B^T X) * A) and <D, D> = scale^2 * sum((B^T B) * (A A^T)).
    """
    difference = gguf.dequantize(final_tensor.data, final_tensor.tensor_type).astype(np.float32, copy=False)
    difference -= gguf.dequantize(base_tensor.data, base_tensor.tensor_type).astype(np.float32, copy=False)
    a32, b32 = a.astype(np.float32), b.astype(np.float32)
    along = scale * float(np.sum((b32.T @ difference).astype(np.float64) * a32))
    a64, b64 = a.astype(np.float64), b.astype(np.float64)
    delta_norm = scale * scale * float(np.sum((b64.T @ b64) * (a64 @ a64.T)))
    difference_norm = float(np.dot(difference.reshape(-1).astype(np.float64), difference.reshape(-1).astype(np.float64)))
    if not delta_norm > 0:
        raise ExportError("The LoRA delta of a target is zero.")
    cosine = along / np.sqrt(delta_norm * difference_norm) if difference_norm > 0 else 0.0
    return along / delta_norm, float(cosine)


def template_identity(reader, tokenizer_config: Path) -> dict:
    """Check that the GGUF chat template is the training template of the pinned tokenizer config."""
    embedded = _field(reader, "tokenizer.chat_template")
    try:
        training = json.loads(tokenizer_config.read_text(encoding="utf-8")).get("chat_template")
    except (OSError, ValueError, AttributeError) as error:
        raise ExportError(f"The pinned tokenizer config could not be read: {tokenizer_config}") from error
    if not isinstance(embedded, str) or embedded != training:
        raise ExportError("The GGUF chat template differs from the pinned training tokenizer config.")
    return {"sha256": hashlib.sha256(embedded.encode("utf-8")).hexdigest(), "matches_base_config": True}


def verify_export(gguf, base, adapter, output: Path, targets: set[str], *, rank: int, alpha: float) -> dict:
    """Check the final GGUF against the base and the adapter; return the retention summary for the receipt."""
    with _temporary_gguf(gguf, output) as final:
        return _check_final(gguf, base, adapter, final, targets, rank=rank, alpha=alpha)


def _check_final(gguf, base, adapter, final, targets: set[str], *, rank: int, alpha: float) -> dict:
    if final.alignment != base.alignment or final.endianess != base.endianess:
        raise ExportError("The final model changed the base alignment or byte order.")
    for name, field in base.fields.items():
        if name.startswith("GGUF."):
            continue
        other = final.fields.get(name)
        if other is None or field.types != other.types or field.contents() != other.contents():
            raise ExportError(f"The final model changed base metadata {name}.")

    actual = _tensor_map(final)
    expected = _tensor_map(base)
    pairs = _tensor_map(adapter)
    if actual.keys() != expected.keys():
        raise ExportError("The final model does not have the same tensors as the base.")
    projections, cosines = [], []
    for name, original in expected.items():
        tensor = actual[name]
        if not np.array_equal(tensor.shape, original.shape):
            raise ExportError(f"The final tensor {name} changed shape.")
        if name in targets:
            if tensor.tensor_type != _target_type(gguf):
                raise ExportError(f"The final LoRA target {name} is not stored as Q8_0.")
            kept, cosine = retention(gguf, original, tensor, pairs[name + ".lora_a"].data,
                                     pairs[name + ".lora_b"].data, alpha / rank)
            if not kept >= MIN_RETENTION:
                raise ExportError(f"The final LoRA target {name} kept {kept:.2f} of the LoRA delta "
                                  f"(at least {MIN_RETENTION:.2f} is required).")
            projections.append(kept)
            cosines.append(cosine)
            continue
        if tensor.tensor_type != original.tensor_type:
            raise ExportError(f"The final tensor {name} changed type.")
        before = original.data.view(np.uint8).reshape(-1)
        after = tensor.data.view(np.uint8).reshape(-1)
        offsets = range(0, len(before), COMPARE_CHUNK)
        if not all(np.array_equal(before[i:i + COMPARE_CHUNK], after[i:i + COMPARE_CHUNK]) for i in offsets):
            raise ExportError(f"The final unmodified tensor {name} changed.")
    if len(projections) != len(targets):
        raise ExportError("The final model is missing a LoRA target.")
    return {"targets": len(projections), "threshold": MIN_RETENTION,
            "min_projection": round(min(projections), 4), "mean_projection": round(float(np.mean(projections)), 4),
            "min_cosine": round(min(cosines), 4)}


def _check_disk(work: Path, output_parent: Path, work_bytes: int, output_bytes: int) -> None:
    needs = [(work, work_bytes), (output_parent, output_bytes)]
    if work.stat().st_dev == output_parent.stat().st_dev:
        needs = [(work, work_bytes + output_bytes)]
    for path, size in needs:
        free = shutil.disk_usage(path).free
        if free < size:
            raise ExportError(f"{path} needs {size / 1024**3:.2f} GiB free and has {free / 1024**3:.2f} GiB.")


def _run_command(command: list[str], stage: str, env: dict) -> None:
    print(stage + ".", flush=True)
    try:
        subprocess.run(command, env=env, check=True, timeout=STAGE_TIMEOUT)
    except subprocess.TimeoutExpired as error:
        raise ExportError(f"{stage} exceeded one hour.") from error
    except subprocess.CalledProcessError as error:
        raise ExportError(f"{stage} failed (exit code {error.returncode}).") from error


RECEIPT_SCHEMA = "reviewer-export/2"


def _receipt(digest: str, retained: dict, template: dict) -> dict:
    return {"schema": RECEIPT_SCHEMA, "base_sha256": BASE_GGUF_SHA, "adapter_sha256": ADAPTER_WEIGHTS_SHA,
            "gguf_sha256": digest, "target_type": "q8_0", "retention": retained, "chat_template": template}


def _valid_receipt(receipt: dict) -> bool:
    digest = receipt.get("gguf_sha256")
    retained = receipt.get("retention")
    template = receipt.get("chat_template")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        return False
    if not isinstance(retained, dict) or not isinstance(template, dict):
        return False
    minimum = retained.get("min_projection")
    if isinstance(minimum, bool) or not isinstance(minimum, (int, float)) or not minimum >= MIN_RETENTION:
        return False
    if template.get("matches_base_config") is not True or not re.fullmatch(r"[0-9a-f]{64}", str(template.get("sha256"))):
        return False
    return receipt == _receipt(digest, retained, template)


def _read_bundle(output: Path) -> str:
    try:
        receipt = json.loads((output / RECEIPT_FILE).read_text(encoding="utf-8"))
        if not isinstance(receipt, dict) or not _valid_receipt(receipt):
            raise ValueError("invalid export receipt")
        digest = receipt["gguf_sha256"]
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise ExportError(f"The existing directory has no matching reviewer export receipt: {output}") from error
    try:
        recipe = (output / "Modelfile").read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ExportError(f"The published Modelfile could not be read: {output}") from error
    if recipe != modelfile(Path(MODEL_FILE)):
        raise ExportError(f"The published Modelfile no longer matches this reviewer export: {output}")
    return digest


def _ollama_show(name: str) -> dict | None:
    try:
        return client._call("POST", "/api/show", {"model": name}, timeout=10.0)
    except client.OllamaError as error:
        if error.code == "not_found":
            return None
        raise ExportError(f"Could not inspect the local Ollama model {name}: {error}") from error


def _matches_model(details: dict | None, digest: str) -> bool:
    lines = str((details or {}).get("modelfile") or "").splitlines()
    source = next((line[5:].strip().strip('"') for line in lines if line.startswith("FROM ")), "")
    parameters = {parts[1]: parts[2] for line in lines if line.startswith("PARAMETER ")
                  if len(parts := line.split(maxsplit=2)) == 3}
    return (source.replace("\\", "/").rsplit("/", 1)[-1] == f"sha256-{digest}"
            and parameters == {"num_ctx": "4096", "temperature": "0"}
            and not any(line.startswith(("SYSTEM ", "ADAPTER ")) for line in lines))


def _import_verified_model(ollama: str, name: str, model: Path, digest: str, env: dict) -> None:
    _require_sha256(model, digest)
    lock_name = hashlib.sha256(name.encode()).hexdigest()[:24]
    try:
        with oslock.FileLock(Path(tempfile.gettempdir()) / f"aivf-export-{lock_name}.lock", note=False):
            _publish_ollama_model(ollama, name, model, digest, env)
    except oslock.Busy as error:
        raise ExportError(f"Another export is registering {name}. Repeat the command when it finishes.") from error


def _publish_ollama_model(ollama: str, name: str, model: Path, digest: str, env: dict) -> None:

    def already_registered() -> bool:
        existing = _ollama_show(name)
        if existing is None:
            return False
        if not _matches_model(existing, digest):
            raise ExportError(f"Ollama model {name} already refers to different weights or settings.")
        return True

    if already_registered():
        return
    staged = "aivf/reviewer-export-" + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="aivf-reviewer-import-") as temporary:
        recipe = Path(temporary) / "Modelfile"
        recipe.write_text(modelfile(model.resolve()), encoding="utf-8", newline="\n")
        wait_for_publish = False
        try:
            _run_command([ollama, "create", staged, "-f", str(recipe)], "Staging the local reviewer", env)
            if not _matches_model(_ollama_show(staged), digest):
                raise ExportError("The staged Ollama model did not match the verified weights and settings.")
            if not already_registered():
                _run_command([ollama, "cp", staged, name], f"Registering the local Ollama model {name}", env)
            if not _matches_model(_ollama_show(name), digest):
                raise ExportError(f"Ollama did not confirm the verified model at {name}.")
        except ExportError as error:
            wait_for_publish = isinstance(error.__cause__, subprocess.TimeoutExpired)
            raise
        finally:
            try:
                deadline = time.monotonic() + (3.0 if wait_for_publish else 0.0)
                while True:
                    if _ollama_show(staged) is not None:
                        _run_command([ollama, "rm", staged], "Removing the temporary reviewer tag", env)
                        break
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.15)
            except (ExportError, OSError) as error:
                print(f"Could not remove the temporary Ollama tag {staged}: {error}", file=sys.stderr)


def export_model(adapter: Path, base_config: Path, base_gguf: Path, name: str, output_dir: Path,
                 *, work_dir: Path | None = None) -> Path:
    weights = adapter / "adapter_model.safetensors"
    _require_sha256(weights, ADAPTER_WEIGHTS_SHA)
    output_dir = output_dir.resolve()
    work_dir = (work_dir or Path(tempfile.gettempdir())).resolve()
    if output_dir == work_dir or output_dir in work_dir.parents:
        raise ExportError(f"Choose an output directory separate from the scratch directory: {output_dir}")

    ollama_url = client.base_url()
    host = urlsplit(ollama_url)
    if host.scheme != "http" or host.hostname not in {"127.0.0.1", "::1", "localhost"} or host.username or host.path or host.query or host.fragment:
        raise ExportError("OLLAMA_HOST must name a local loopback HTTP server for this export.")
    ollama = server.executable()
    if ollama is None:
        raise ExportError("Ollama is not installed locally.")
    env = gguf_convert._env()
    import_env = {**env, "OLLAMA_HOST": ollama_url}
    if output_dir.exists():
        output = output_dir / MODEL_FILE
        _import_verified_model(ollama, name, output, _read_bundle(output_dir), import_env)
        return output

    gguf = _load_gguf()

    base_identity = file_identity(base_gguf)
    _require_sha256(base_gguf, BASE_GGUF_SHA)
    base = gguf.GGUFReader(base_gguf)
    if file_identity(base_gguf) != base_identity:
        raise ExportError("The local base changed during verification.")
    if _field(base, "general.file_type") != gguf.LlamaFileType.MOSTLY_Q4_K_M:
        raise ExportError("The accepted local base must use Q4_K_M.")
    targets = {f"blk.{layer}.{module}.weight" for layer in range(28) for module in TARGET_MODULES}

    # Scratch holds the adapter copies and its GGUF; the final file has Q8_0 targets (34 bytes per 32 weights).
    work_dir.mkdir(parents=True, exist_ok=True)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    final_bytes = base.data_offset + sum(int(t.n_elements) // 32 * 34 if t.name in targets else int(t.n_bytes) for t in base.tensors)
    _check_disk(work_dir, output_dir.parent, 2 * weights.stat().st_size + DISK_MARGIN, final_bytes + DISK_MARGIN)

    with tempfile.TemporaryDirectory(prefix="aivf-reviewer-", dir=work_dir) as scratch:
        work = Path(scratch)
        # Snapshot the allowlisted inputs. The normalized adapter name also keeps the
        # converter's GGUF metadata and output digest stable across source locations.
        local_adapter = work / "adapter_a"
        local_adapter.mkdir()
        for filename, digest in ((weights.name, ADAPTER_WEIGHTS_SHA), ("adapter_config.json", ADAPTER_CONFIG_SHA),
                                 ("README.md", ADAPTER_CARD_SHA)):
            path = local_adapter / filename
            shutil.copyfile(adapter / filename, path)
            _require_sha256(path, digest)
        local_base_config = work / "base-config"
        local_base_config.mkdir()
        for filename, digest in BASE_CONFIG_SHA.items():
            path = local_base_config / filename
            shutil.copyfile(base_config / filename, path)
            _require_sha256(path, digest)

        converted = work / "adapter-v40a-f16.gguf"
        _run_command(convert_command(local_adapter, local_base_config, converted), "Converting the v40 LoRA", env)
        _require_sha256(converted, CONVERTED_ADAPTER_SHA)

        with _temporary_gguf(gguf, converted) as patch, \
                tempfile.TemporaryDirectory(prefix=".aivf-reviewer-", dir=output_dir.parent) as staging:
            stage = Path(staging)
            final = stage / MODEL_FILE
            print("Merging the LoRA into Q8_0 targets.", flush=True)
            merge_adapter(gguf, base, patch, final, expected_targets=targets, rank=64, alpha=128)
            print("Verifying the final GGUF.", flush=True)
            retained = verify_export(gguf, base, patch, final, targets, rank=64, alpha=128)
            with _temporary_gguf(gguf, final) as written:
                template = template_identity(written, local_base_config / "tokenizer_config.json")
            if file_identity(base_gguf) != base_identity:
                raise ExportError("The local base changed during export.")
            digest = gguf_convert._sha256(final)
            print(f"Kept at least {retained['min_projection']:.3f} of the LoRA delta in every target "
                  f"(mean {retained['mean_projection']:.3f}, minimum cosine {retained['min_cosine']:.3f}).", flush=True)
            (stage / "Modelfile").write_text(modelfile(Path(MODEL_FILE)), encoding="utf-8", newline="\n")
            receipt = _receipt(digest, retained, template)
            (stage / RECEIPT_FILE).write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
            stage.rename(output_dir)

    output = output_dir / MODEL_FILE
    try:
        _import_verified_model(ollama, name, output, digest, import_env)
    except (ExportError, OSError) as error:
        raise ExportError(f"{error} The verified export is available at {output_dir}. Repeat the same command to retry import.") from error
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", required=True, type=Path, help="Local accepted v40 stage A PEFT adapter folder")
    parser.add_argument("--base-config", required=True, type=Path, help="Local pinned Qwen2.5-Coder-7B-Instruct config folder")
    parser.add_argument("--base-gguf", required=True, type=Path, help="Local accepted Qwen2.5-Coder 7b Ollama GGUF or blob")
    parser.add_argument("--name", default="aivf/reviewer-v40", help="Name to create in the local Ollama server")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT, type=Path, help="Directory for the GGUF, Modelfile and export receipt")
    parser.add_argument("--work-dir", type=Path, help="Directory for scratch space (default: system temp directory)")
    args = parser.parse_args(argv)
    try:
        output = export_model(args.adapter, args.base_config, args.base_gguf, args.name, args.output_dir,
                              work_dir=args.work_dir)
    except (ExportError, ImportError, OSError, ValueError, OverflowError) as error:
        parser.exit(1, f"Export failed: {error}\n")
    print(f"Created {args.name} from {output}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
