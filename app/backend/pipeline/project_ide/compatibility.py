"""Evidence and admission checks for the explicitly selected OpenCode agent."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re


REQUIRED_CHECKS = (
    "artifact_provenance", "runtime_contract", "native_tool_use", "continued_tool_use",
    "skill_loading", "read_search_edit_test_repair", "structured_assessment",
    "insufficient_context", "permissions", "questions", "cancellation", "recovery",
    "windows_paths", "resource_queue",
)


class GateError(ValueError):
    """Missing or conflicting evidence; never a reason to substitute a model."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(path: Path) -> dict:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GateError(f"Cannot read {path.name}: {type(exc).__name__}") from exc
    if not isinstance(result, dict):
        raise GateError(f"{path.name} must contain an object.")
    return result


def inspect_artifact(run_dir: str | Path) -> dict:
    """Verify the saved training identity and bytes without loading model code or pickle."""
    root = Path(run_dir).resolve()
    manifest = _json(root / "outputs_manifest.json")
    if manifest.get("schema") != "run-outputs/1":
        raise GateError("The training outputs manifest has an unsupported schema.")
    verified = {}
    for name in ("run_config.json", "adapter/adapter_config.json", "adapter/adapter_model.safetensors"):
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise GateError(f"Missing or outside-artifact file: {name}")
        record = manifest.get("files", {}).get(name)
        if not isinstance(record, dict) or not record.get("sha256"):
            raise GateError(f"No provenance receipt for {name}")
        digest = sha256(path)
        if path.stat().st_size != record.get("bytes") or digest != record["sha256"]:
            raise GateError(f"The bytes of {name} do not match the training manifest.")
        verified[name] = digest
    config = _json(root / "run_config.json")
    adapter = _json(root / "adapter/adapter_config.json")
    if config.get("state") != "finished" or config.get("smoke_test") is not False:
        raise GateError("The artifact must come from a finished, non-smoke training run.")
    base, revision = config.get("model"), config.get("model_revision")
    if not isinstance(base, str) or not base or not re.fullmatch(r"[0-9a-f]{40}", str(revision or "")):
        raise GateError("The training run must identify a base model and pinned revision.")
    if adapter.get("base_model_name_or_path") != base:
        raise GateError("The adapter and training run name different base models.")
    if adapter.get("peft_type") != "LORA" or adapter.get("task_type") != "CAUSAL_LM":
        raise GateError("The main agent requires a generative LoRA artifact.")
    return {
        "run_dir": str(root), "base_model": base, "base_revision": revision,
        "adapter_dir": str(root / "adapter"), "rank": adapter.get("r"),
        "adapter_sha256": verified["adapter/adapter_model.safetensors"],
        "run_config_sha256": verified["run_config.json"],
        "manifest_sha256": sha256(root / "outputs_manifest.json"),
    }


def inspect_base_model(model: str | Path) -> dict:
    """Validate the user's explicitly selected temporary base agent, without LoRA."""
    model = Path(model).resolve()
    receipt = _json(model.with_name("conversion.json"))
    source = receipt.get("source", {})
    if source.get("id") != "Qwen/Qwen2.5-Coder-7B-Instruct" or not re.fullmatch(r"[0-9a-f]{40}", str(source.get("revision", ""))):
        raise GateError("Select the pinned Qwen/Qwen2.5-Coder-7B-Instruct conversion explicitly.")
    record = next((f for f in receipt.get("files", []) if f.get("name") == model.name), None)
    if record is None or not model.is_file() or sha256(model) != record.get("sha256"):
        raise GateError("The selected base model bytes do not match the conversion receipt.")
    return {"selection": "explicit_base_model", "base_model": source["id"], "base_revision": source["revision"],
            "model_path": str(model), "model_sha256": record["sha256"], "adapter_sha256": None}


def verdict(checks: dict) -> dict:
    """Only positive evidence for every required live check opens the next phase."""
    missing = [name for name in REQUIRED_CHECKS if checks.get(name, {}).get("status") != "passed"]
    failed = [name for name in missing if checks.get(name, {}).get("status") == "failed"]
    return {"status": "failed" if failed else "blocked" if missing else "passed", "missing": missing, "failed": failed}


def verify_engine(engine: dict, *, trained_hash: str | None, converted_hash: str | None, model_name: str, context: int) -> None:
    expected = {"adapter_trained_from": trained_hash, "adapter_sha256": converted_hash,
                "model_file": model_name, "context": context}
    for key, value in expected.items():
        if engine.get(key) != value:
            raise GateError(f"The loaded gateway has a different or missing {key} attestation.")


def evaluate_native_probe(messages: list[dict], marker: str, *, timed_out: bool = False) -> dict:
    """Require real completed calls and a later read after a returned search result."""
    rounds = []
    text = []
    errors = []
    assistants = [m for m in messages if m.get("info", {}).get("role") == "assistant"]
    final = assistants[-1].get("info", {}) if assistants else {}
    complete = bool(final.get("finish") == "stop" and final.get("time", {}).get("completed") and not timed_out)
    for message in messages:
        info = message.get("info", {})
        if info.get("role") != "assistant":
            continue
        if info.get("error"):
            errors.append(info["error"].get("name", "runtime_error"))
        if any(p.get("type") == "tool" and p.get("state", {}).get("status") != "completed"
               for p in message.get("parts", [])):
            complete = False
        calls = [p.get("tool") for p in message.get("parts", [])
                 if p.get("type") == "tool" and p.get("state", {}).get("status") == "completed"]
        if calls:
            rounds.append(calls)
        text.extend(p.get("text", "") for p in message.get("parts", []) if p.get("type") == "text")
    names = [name for group in rounds for name in group]
    continued = any("glob" in group and any("read" in later for later in rounds[i + 1:])
                    for i, group in enumerate(rounds))
    return {
        "native_tool_use": {"status": "passed" if complete and not errors and "read" in names and marker in "\n".join(text) else "failed",
                            "completed_tools": names, "errors": errors, "completed": complete, "timed_out": timed_out},
        "continued_tool_use": {"status": "passed" if complete and not errors and continued else "failed", "tool_rounds": rounds},
    }


def runtime_environment(state_dir: str | Path, inherited: dict | None = None) -> dict:
    """Give OpenCode private discovery roots and only the system environment it needs."""
    from app.backend.pipeline.agentic.exec import toolchain

    inherited = os.environ if inherited is None else inherited
    keep = {
        "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP",
        "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA",
        "LOCALAPPDATA", "APPDATA", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "OS", "LANG",
    }
    env = {name: value for name, value in inherited.items() if name.upper() in keep}
    if ripgrep := toolchain.find("ripgrep"):
        key = next((name for name in env if name.upper() == "PATH"), "PATH")
        env[key] = os.pathsep.join(filter(None, (str(ripgrep.resolve().parent), env.get(key, ""))))
    root = Path(state_dir).resolve()
    for variable, folder in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                             ("XDG_CACHE_HOME", "cache"), ("XDG_STATE_HOME", "state")):
        env[variable] = str(root / folder)
    env.update({
        "OPENCODE_CONFIG_DIR": str(root / "config" / "opencode"),
        "OPENCODE_DB": str(root / "data" / "opencode.sqlite"),
        "OPENCODE_CONFIG_CONTENT": "{}",
        "OPENCODE_DISABLE_PROJECT_CONFIG": "true", "OPENCODE_DISABLE_EXTERNAL_SKILLS": "true",
        "OPENCODE_DISABLE_CLAUDE_CODE": "true", "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true",
        "OPENCODE_DISABLE_MODELS_FETCH": "true", "OPENCODE_DISABLE_AUTOUPDATE": "true",
        "OPENCODE_DISABLE_LSP_DOWNLOAD": "true", "OPENCODE_CLIENT": "aivf-projects-ide-gate",
        "OPENCODE_ENABLE_QUESTION_TOOL": "true", "NO_COLOR": "1",
    })
    return env
