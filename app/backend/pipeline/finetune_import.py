"""Importing the outputs of a fine-tune run (``outputs.zip``, written by ``training/finetune/runner.py``). No torch.

Everything is checked before anything is kept; one failed check rejects the whole import:

* ``zip_structure``: no path that leaves the import folder, no duplicate names, a bounded size and file count;
* ``manifest``: every file the manifest lists is present with its sha256 and size, nothing else is in the zip, the required files exist;
* ``run_config`` and ``identity``: a fine-tune run config that names the pinned model and tokenizer commits, the dataset version and
  manifest hash, the hash of the resolved config, the bundle it ran from (its manifest hash) and the serializer of the prepared run;
* ``phase``: the dataset-fine-tuning phase with RAG off, zero retrieval and embedding calls, no RAG library loaded, the phase and input
  policy hashes of the prepared run;
* ``head``: the decision head's label order and pooling are the contract's;
* ``adapter``: the contract's rank, alpha and target modules, a parameter count equal to the one the run reports and, for a full run,
  equal to the adapter size of the pinned 7B base (so a tiny model dressed as a training run is refused);
* ``consistency``: the metric lines match the steps done, the evaluations and the calibration parse;
* ``type``: a full run (not a smoke test) needs a GPU, and is typed from its purpose and state.

These checks give integrity and consistency, not proof that a run happened: the claims in ``run_config.json`` come from whoever ran it.
An accepted artifact is typed ``software_smoke`` (a tiny model; never moves a run's state), ``gpu_smoke`` (a short run on the pinned base),
``trained`` (a finished run) or ``incomplete`` (failed or interrupted: kept as evidence, never a result). The run state is reached only
through these types (see ``finetune_prep.run_state``).
"""
from __future__ import annotations
from app.backend.pipeline.finetune_deletion import guarded

import hashlib
import json
import os
import pathlib
import shutil
import zipfile

from training.finetune import adapter_math, config as cfgmod, resolve as rs, serialization as sz
from app.backend.pipeline import util

SCHEMA = "finetune-import/1"
RUN_SCHEMA = "finetune-run/1"
OUTPUTS_SCHEMA = "run-outputs/1"
POOLING = "last_prompt_position"                           # head.POOLING (head.py needs torch; tests check they agree)
LOSS_MODES = ("per_example_completion_ce", "present_field_normalized_ce")    # losses.MODES (losses.py needs torch)
MAX_BYTES = 4 * 2 ** 30
MAX_FILES = 2000
REQUIRED = ("run_config.json", "decision_head.pt", "calibration.json", "metrics.jsonl", "evaluations.json",
            "adapter/adapter_config.json", "adapter/adapter_model.safetensors")
CHECKS = ("zip_structure", "manifest", "run_config", "identity", "head", "adapter", "consistency", "type")


class ImportRejected(ValueError):
    """The artifact failed one or more checks; ``failures`` lists them and nothing was kept."""

    def __init__(self, failures: list[dict]):
        self.failures = failures
        super().__init__("; ".join(f"{f['check']}: {f['detail']}" for f in failures))


def _sha_file(path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe(name: str) -> bool:
    parts = pathlib.PurePosixPath(name.replace("\\", "/")).parts
    return bool(parts) and not name.startswith(("/", "\\")) and ".." not in parts and ":" not in parts[0]


def list_imports(run_dir) -> list[dict]:
    root = pathlib.Path(run_dir) / "imports"
    out = []
    for d in sorted(root.iterdir()) if root.exists() else []:
        try:
            out.append(json.loads((d / "import.json").read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda r: (r.get("imported_at", ""), r.get("import_id", "")))


# -- the checks ------------------------------------------------------------------------------------------------------------------------------------------

def _zip_structure(z: zipfile.ZipFile, max_bytes: int) -> list[dict]:
    infos = z.infolist()
    bad = lambda detail: [{"check": "zip_structure", "detail": detail}]
    names = [i.filename for i in infos]
    unsafe = [n for n in names if not _safe(n)]
    if unsafe:
        return bad(f"a path that would leave the import folder: {unsafe[0]}")
    if len(set(names)) != len(names):
        return bad("the artifact lists a file name twice")
    if len(infos) > MAX_FILES:
        return bad(f"{len(infos)} files, over the limit of {MAX_FILES}")
    total = sum(i.file_size for i in infos)
    if total > max_bytes:
        return bad(f"the artifact would unpack to {total:,} bytes (size limit {max_bytes:,})")
    return []


def _manifest(folder: pathlib.Path, names: set[str]) -> tuple[list[dict], dict | None]:
    fail = lambda detail: {"check": "manifest", "detail": detail}
    manifest = util.read_json(folder / "outputs_manifest.json")
    if not isinstance(manifest, dict) or manifest.get("schema") != OUTPUTS_SCHEMA or not isinstance(manifest.get("files"), dict):
        return [fail(f"outputs_manifest.json is missing or is not a {OUTPUTS_SCHEMA} file")], None
    listed, out = manifest["files"], []
    for name, info in sorted(listed.items()):
        if name not in names:
            out.append(fail(f"{name} is missing from the artifact"))
        elif _sha_file(folder / name) != info.get("sha256") or (folder / name).stat().st_size != info.get("bytes"):
            out.append(fail(f"{name} changed after the run wrote it"))
    out += [fail(f"{n} is in the artifact but not in its manifest") for n in sorted(names - set(listed) - {"outputs_manifest.json"})]
    out += [fail(f"the required file {n} is not listed in the manifest") for n in REQUIRED if n not in listed]
    return out, manifest


def _expected_identity(run_dir: pathlib.Path) -> dict:
    prepared = util.read_json(run_dir / "prepared.json")
    if not prepared:
        raise ImportRejected([{"check": "identity", "detail": f"{run_dir / 'prepared.json'} does not exist: prepare the run first"}])
    resolved = json.loads((run_dir / rs.FILE).read_text(encoding="utf-8"))
    cfg = rs.read_resolved(run_dir)
    with zipfile.ZipFile(run_dir / "bundle.zip") as z:
        bundle_manifest = _sha(z.read("bundle_manifest.json"))
    return {"prepared": prepared, "resolved": resolved, "cfg": cfg, "bundle_manifest_sha256": bundle_manifest,
            "resolved_config_sha256": _sha((run_dir / rs.FILE).read_bytes())}


def _identity(rc: dict, exp: dict) -> list[dict]:
    p, fail = exp["prepared"], lambda detail: {"check": "identity", "detail": detail}
    out = []
    for key, want in (("model", p["model"]), ("model_revision", p["model_revision"]), ("tokenizer_revision", p["tokenizer_revision"]),
                      ("resolved_config_sha256", exp["resolved_config_sha256"])):
        if rc.get(key) != want:
            out.append(fail(f"{key} is {rc.get(key)!r}, the prepared run's is {want!r}"))
    d = rc.get("dataset") or {}
    if d.get("version") != p["dataset"]["tag"] or d.get("manifest_sha256") != p["dataset"]["manifest_sha256"]:
        out.append(fail(f"the dataset ({d.get('version')!r}, manifest {str(d.get('manifest_sha256'))[:12]}) is not the prepared run's "
                        f"({p['dataset']['tag']!r}, manifest {p['dataset']['manifest_sha256'][:12]})"))
    bundle = rc.get("bundle")
    if not bundle:
        out.append(fail("the run did not come from a bundle (no bundle manifest hash is recorded)"))
    elif bundle.get("manifest_sha256") != exp["bundle_manifest_sha256"]:
        out.append(fail("the run came from another bundle than the one exported for this run (the bundle manifest hash differs)"))
    prompt = rc.get('prompt_text') or {}
    protocol = exp['cfg'].model.serialization
    if protocol == sz.CHAT_ID:
        from types import SimpleNamespace
        template = prompt.get('chat_template')
        # Hash the recorded, pinned tokenizer template without executing imported Jinja.
        tokenizer = SimpleNamespace(chat_template=template, apply_chat_template=lambda *a, **k: '')
        expected_hash = sz.serialization_hash(protocol, tokenizer) if isinstance(template, str) and template else None
    else:
        expected_hash = sz.serialization_hash(protocol)
    if prompt.get('serialization', protocol) != protocol or prompt.get("serialization_hash") != expected_hash:
        out.append(fail("the run used another serializer than this version of the code (the serialization hash differs)"))
    if rc.get("config_overridden"):
        out.append(fail("the run used a config that differs from the resolved config of the run"))
    return out


def _head(rc: dict, cfg) -> list[dict]:
    fail = lambda detail: {"check": "head", "detail": detail}
    h = rc.get("head") or {}
    out = []
    if h.get("label_order") != list(cfg.loss.decision_head.classes):
        out.append(fail(f"the head's label order {h.get('label_order')} is not the contract's {list(cfg.loss.decision_head.classes)}"))
    if h.get("pooling") != POOLING:
        out.append(fail(f"the head's pooling is {h.get('pooling')!r}, not {POOLING!r}"))
    if h.get("hidden_size") != (rc.get("model_loaded") or {}).get("hidden_size"):
        out.append(fail("the head's size does not match the model's hidden size"))
    return out


def expected_architecture(rc: dict, given=None) -> tuple[object | None, str]:
    """``(architecture, where it comes from)`` for the model a run says it trained. A caller may give one. The pinned 7B base has its published architecture, which no claim of
    the run can change; any other model has the one the run reported about the model it loaded; with neither, None (the size of its adapter cannot be checked)."""
    if given is not None:
        return given, "given"
    if rc.get("model") == cfgmod.BASE_MODEL_ID:
        return adapter_math.QWEN2_5_CODER_7B, "the published architecture of the pinned base"
    reported = adapter_math.architecture_from(rc.get("model_loaded"))
    return (reported, "the architecture the run reported for its model") if reported is not None else (None, "unknown")


def _adapter(folder: pathlib.Path, rc: dict, cfg, architecture) -> tuple[list[dict], int | None]:
    fail = lambda detail: {"check": "adapter", "detail": detail}
    ac = util.read_json(folder / "adapter" / "adapter_config.json")
    if not isinstance(ac, dict):
        return [fail("adapter/adapter_config.json is missing or does not parse")], None
    out = []
    if ac.get("r") != cfg.lora.r:
        out.append(fail(f"the adapter rank is {ac.get('r')}, the contract's is {cfg.lora.r}"))
    if ac.get("lora_alpha") != cfg.lora.lora_alpha:
        out.append(fail(f"the adapter alpha is {ac.get('lora_alpha')}, the contract's is {cfg.lora.lora_alpha}"))
    if set(ac.get("target_modules") or []) != set(cfg.lora.target_modules):
        out.append(fail(f"the adapter's target modules {sorted(ac.get('target_modules') or [])} are not the contract's "
                        f"{sorted(cfg.lora.target_modules)}"))
    if ac.get("bias") != cfg.lora.bias or bool(ac.get("use_rslora")) != cfg.lora.use_rslora:
        out.append(fail("the adapter's bias or rslora setting is not the contract's"))
    if bool(ac.get("use_dora")) != cfg.lora.use_dora:
        out.append(fail(f"the adapter {'uses' if ac.get('use_dora') else 'does not use'} DoRA, the contract's setting is {'on' if cfg.lora.use_dora else 'off'}"))
    try:
        n = adapter_math.safetensors_parameters(folder / "adapter" / "adapter_model.safetensors")
    except (ValueError, OSError, KeyError) as e:
        return out + [fail(f"the adapter file cannot be read: {e}")], None
    claimed = (rc.get("adapter") or {}).get("trainable_parameters")
    if n != claimed:
        out.append(fail(f"the adapter file holds {n:,} parameters but the run config says {claimed!r}"))
    if rc.get("smoke_test") is False and architecture is not None:
        want = adapter_math.expected_adapter_parameters(architecture, cfg.lora.r, cfg.lora.target_modules, cfg.lora.use_dora)
        if n != want:
            who = "the pinned base" if rc.get("model") == cfgmod.BASE_MODEL_ID else str(rc.get("model"))
            out.append(fail(f"a run on {who} has an adapter of {want:,} parameters; this one has {n:,}"))
    return out, n


def _consistency(folder: pathlib.Path, rc: dict) -> list[dict]:
    fail = lambda detail: {"check": "consistency", "detail": detail}
    out = []
    lines = [l for l in (folder / "metrics.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    if rc.get("steps_done") is not None and len(lines) != rc["steps_done"]:
        out.append(fail(f"metrics.jsonl has {len(lines)} lines but the run config says {rc['steps_done']} steps were done"))
    mode = (rc.get("loss") or {}).get("mode")
    if mode not in LOSS_MODES:
        out.append(fail(f"the loss mode {mode!r} is not one of {LOSS_MODES}"))
    if not isinstance(util.read_json(folder / "evaluations.json"), list):
        out.append(fail("evaluations.json is not a list"))
    cal = util.read_json(folder / "calibration.json")
    if not isinstance(cal, dict) or "status" not in cal:
        out.append(fail("calibration.json does not parse or has no status"))
    return out


def _type(rc: dict) -> tuple[str | None, list[dict]]:
    fail = lambda detail: {"check": "type", "detail": detail}
    if rc.get("smoke_test"):
        return "software_smoke", []
    if rc.get("state") not in ("finished", "early_stopped"):
        return "incomplete", []
    tpu = (rc.get("environment") or {}).get("backend") == "tpu" and str(rc.get("device", "")).startswith("xla")
    if not (rc.get("environment") or {}).get("cuda") and not tpu:
        return None, [fail("a full run on the pinned base needs a GPU; this run reports none (environment.cuda is false)")]
    return (("tpu_smoke" if tpu else "gpu_smoke") if rc.get("purpose") == "smoke" else "trained"), []


def _analytics(folder, rc):
    from training.finetune.analytics import read_report_header, SCHEMA as ANALYTICS_SCHEMA
    report = folder / "training_analytics.json"
    if not report.exists():
        return []
    try:
        header = read_report_header(report)
        provenance = header.get("provenance", {})
        if header.get("schema") != ANALYTICS_SCHEMA:
            raise ValueError("unsupported analytics schema")
        for key in ("model_revision", "tokenizer_revision", "resolved_config_sha256", "config_sha256", "dataset", "bundle", "stage"):
            if provenance.get(key) != rc.get(key):
                raise ValueError(f"analytics {key} does not match run_config")
        return []
    except (ValueError, OSError, TypeError, AttributeError) as error:
        return [{"check": "analytics", "detail": str(error)}]


# -- the import ----------------------------------------------------------------------------------------------------------------------------------------------

@guarded
def import_outputs(zip_path, run_dir, *, architecture=None, max_bytes: int = MAX_BYTES, dry_run: bool = False) -> dict:
    """Check an ``outputs.zip`` against the prepared run and keep it under ``<run>/imports/<id>/``. Raises ``ImportRejected``. ``architecture`` is the architecture the adapter
    must fit; without it the run's model decides (``expected_architecture``). With ``dry_run`` every check runs on a copy that is thrown away and nothing is kept."""
    zip_path, run_dir = pathlib.Path(zip_path), pathlib.Path(run_dir)
    sha = _sha_file(zip_path)
    import_id = sha[:16]
    root = run_dir / "imports"
    final, tmp = root / import_id, root / (f".check-{import_id}" if dry_run else f".tmp-{import_id}")
    if (final / "import.json").exists():
        return {**json.loads((final / "import.json").read_text(encoding="utf-8")), "already_imported": True}
    exp = _expected_identity(run_dir)
    with zipfile.ZipFile(zip_path) as z:
        failures = _zip_structure(z, max_bytes)
        if failures:
            raise ImportRejected(failures)
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True)
        try:
            for info in z.infolist():
                if info.is_dir():
                    continue
                target = tmp / info.filename
                if not target.resolve().is_relative_to(tmp.resolve()):
                    raise ImportRejected([{"check": "zip_structure", "detail": f"a path that would leave the import folder: {info.filename}"}])
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            names = {p.relative_to(tmp).as_posix() for p in tmp.rglob("*") if p.is_file()}
            failures, _ = _manifest(tmp, names)
            if failures:
                raise ImportRejected(failures)
            rc = util.read_json(tmp / "run_config.json")
            if not isinstance(rc, dict) or rc.get("schema") != RUN_SCHEMA or rc.get("task") != "finetune":
                raise ImportRejected([{"check": "run_config", "detail": f"run_config.json is not a {RUN_SCHEMA} fine-tune run config"}])
            kind, type_failures = _type(rc)
            arch, arch_source = expected_architecture(rc, architecture)
            adapter_failures, n_params = _adapter(tmp, rc, exp["cfg"], arch)
            failures = (_identity(rc, exp) + _head(rc, exp["cfg"]) + adapter_failures + _consistency(tmp, rc)
                        + type_failures + _analytics(tmp, rc))
            if failures:
                raise ImportRejected(failures)
            record = {"schema": SCHEMA, "import_id": import_id, "zip_sha256": sha, "imported_at": util.now_utc(), "type": kind,
                      "checks": [{"name": c, "passed": True} for c in CHECKS],
                      "run": {"state": rc.get("state"), "purpose": rc.get("purpose"), "stage": rc.get("stage"),
                              "steps_done": rc.get("steps_done"), "smoke_test": rc.get("smoke_test"),
                              "loss_mode": (rc.get("loss") or {}).get("mode"), "invariance": bool((rc.get("loss") or {}).get("invariance_enabled")),
                              "cuda": (rc.get("environment") or {}).get("cuda"), "dtype": rc.get("dtype"),
                              "parameters": (rc.get("model_loaded") or {}).get("parameters"), "started": rc.get("timestamp")},
                      "adapter": {"sha256": _sha_file(tmp / "adapter" / "adapter_model.safetensors"), "parameters": n_params},
                      "head": {"sha256": _sha_file(tmp / "decision_head.pt")},
                      "calibration": {k: (util.read_json(tmp / "calibration.json") or {}).get(k) for k in ("status", "n_examples", "negatives")},
                      "bundle_manifest_sha256": exp["bundle_manifest_sha256"], "model_revision": rc.get("model_revision"),
                      "dataset": rc.get("dataset"), "warnings": rc.get("warnings") or []}
            if rc.get("smoke_test") is False:                   # what the size of the adapter was held to, so a reader knows what the check was
                record["adapter"]["size_checked_against"] = arch_source
                if arch is None:
                    record["warnings"] = [*record["warnings"], f"The size of the adapter was not checked against the architecture of {rc.get('model')}: the run did not report it."]
            if dry_run:
                shutil.rmtree(tmp, ignore_errors=True)
                return {**record, "already_imported": False, "dry_run": True}
            (tmp / "import.json").write_text(json.dumps(record, indent=1, sort_keys=True) + "\n", encoding="utf-8")
            if final.exists():
                shutil.rmtree(final)
            os.replace(tmp, final)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
    return {**record, "already_imported": False}
