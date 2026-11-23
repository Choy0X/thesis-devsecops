"""Preparing a fine-tune from the app: resolve and pin a run, export the bundle and the notebook, check the bundle on the CPU, and say how
far the run has got. No torch here; the checks run the training package in a child process.

A run folder (``training/runs/<name>/``) holds ``resolved_config.json`` (the pinned model and tokenizer commits, the dataset version and
manifest hash), ``bundle.zip`` (the source package and the data a GPU runtime needs), ``finetune.ipynb`` (made for that bundle's hash),
``prepared.json`` and, after the CPU check, ``cpu_validation.json``.

The CPU check unpacks the bundle into an empty folder and runs ``python -m training.finetune.runner --tiny`` there with only that folder
importable, so it shows the bundle is self-contained (the app is not importable in that process) and that the same code trains a tiny
random model for a few steps. It is a software check: not a training result and not a GPU test.

The state of a run is read from evidence on disk, never from a notebook that exists: ``configured`` (a verified bundle for the resolved
run), ``cpu_validated`` (a passed CPU check of that very bundle). ``gpu_smoke_tested``, ``trained`` and ``evaluated`` need artifacts that
a GPU run produces and the app imports; until then they are listed as blocked with what they need.
"""
from __future__ import annotations

import json
import math
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

from app.backend import compute
from training.finetune import bundle as bd, notebook_gen as ng, resolve as rs
from app.backend.pipeline import util
from app.backend.pipeline.finetune_deletion import guarded

RUNS = pathlib.Path("training/runs")
ROOT = pathlib.Path(__file__).resolve().parents[3]
REQUIREMENTS = ROOT / "training" / "requirements-finetune.txt"
PREPARED = "prepared.json"
VALIDATION = "cpu_validation.json"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
BLOCKED = {"gpu_smoke_tested": "needs the outputs of a short GPU run (purpose smoke) of this bundle, imported into the app",
           "trained": "needs the outputs of a finished full run of this bundle on a GPU (adapter, head, calibration), imported into the app",
           "evaluated": "needs an evaluation run (with metrics) of the imported adapter through the shared evaluation path"}
CPU_NOTE = ("software plumbing check on a tiny random model in a fresh process: not a training result and not a GPU test")


def check_run_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME.match(name):
        raise ValueError(f"a run name is 1 to 64 letters, digits, dots, hyphens or underscores, starting with a letter or digit: {name!r}")
    return name


def _write_json(path: pathlib.Path, obj) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, sort_keys=True, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def export_data_preflight(params: dict) -> dict:
    """The generic job route cannot bypass new-run dataset admission.

    A deliberately requested refresh may reproduce the exact historical data;
    the resolver still refuses changing that run's pinned inputs.
    """
    import hashlib
    from . import versions
    from training.finetune import quality_gate
    name = check_run_name(params.get('run') or 'pilot')
    tag = params.get('tag') or 'preview'
    if not isinstance(tag, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,100}', tag):
        raise ValueError('Invalid dataset version')
    dataset = pathlib.Path(versions.PROC) / f'dataset_{tag}'
    mode, budget = _trial_options(params.get('mode', 'full'), params.get('total_budget_minutes', 30))
    if mode == 'trial':
        from . import trial_data
        assessment = trial_data.capability(dataset)
    else:
        assessment = quality_gate.data_preflight(dataset, purpose=params.get('purpose') or 'train')
    old_path = pathlib.Path(RUNS) / name / PREPARED
    old = util.read_json(old_path) or {}
    refresh = params.get('refresh') is True
    recorded = old.get('source_dataset') or old.get('dataset') or {}
    current = dataset / 'manifest.json'
    exact_refresh = (refresh and old_path.is_file() and recorded.get('tag') == tag and current.is_file()
                     and hashlib.sha256(current.read_bytes()).hexdigest() == recorded.get('manifest_sha256'))
    if not assessment['allowed']:
        if not exact_refresh:
            raise ValueError('Dataset is not ready for ' + ('a portable trial' if mode == 'trial' else 'full training') + ': ' + '; '.join(assessment['blockers']))
    from . import finetune_catalog
    if refresh:
        if not exact_refresh:
            raise ValueError('Explicit refresh requires the original prepared dataset')
        defaults = {'stage': 1, 'purpose': 'train', 'mode': 'full', 'total_budget_minutes': 30}
        fields = {'stage': 'stage', 'purpose': 'purpose', 'steps': 'steps', 'eval_max': 'eval_max', 'eval_views': 'eval_views',
                  'session_minutes': 'session_minutes', 'loss_mode': 'loss_mode', 'overrides': 'overrides', 'profile': 'profile',
                  'revision': 'model_revision', 'tokenizer_revision': 'tokenizer_revision'}
        fields.update(mode='mode', total_budget_minutes='total_budget_minutes')
        for requested, saved in fields.items():
            expected_value = old.get(saved, defaults.get(requested))
            supplied = params.get(requested, defaults.get(requested))
            if requested == 'overrides':
                expected_value, supplied = expected_value or {}, supplied or {}
            if supplied != expected_value:
                raise ValueError('Explicit refresh cannot change the original inputs: ' + requested)
    else:
        if old_path.exists():
            raise ValueError('An existing prepared run requires explicit refresh')
        original = {key: value for key, value in params.items() if key not in ('run', 'parent_run_id', 'creation_request_id')}
        finetune_catalog.validate_creation(RUNS, request_id=params.get('creation_request_id'), name=name,
                                           run_id=params.get('parent_run_id'), params=original)
    expected = params.get('parent_run_id')
    if expected:
        if finetune_catalog.run_identity(RUNS, name) != expected:
            raise ValueError('Run reservation identity mismatch')
    return assessment


def _trial_options(mode, total_budget_minutes):
    if mode not in ('trial', 'full'):
        raise ValueError('mode must be trial or full')
    if (type(total_budget_minutes) not in (int, float) or not math.isfinite(total_budget_minutes)
            or total_budget_minutes <= 0):
        raise ValueError('total_budget_minutes must be positive and finite')
    return mode, float(total_budget_minutes)


# -- prepare ------------------------------------------------------------------------------------------------------------------------------------

def trial_profile():
    """The approved desktop trial recipe, separate from the historical contract."""
    return {'label': 'Portable trial', 'purpose': 'Experimental held-out source learning within a total time budget.',
            'rationale': 'Keep the approved Instruct model, adapter capacity and context; measure runtime resources before execution.',
            'revision': 'c03e6d358207e414f1eca0bb1891e29f1db0e242',
            'tokenizer_revision': 'c03e6d358207e414f1eca0bb1891e29f1db0e242',
            'overrides': {'model.id': 'Qwen/Qwen2.5-Coder-7B-Instruct', 'model.serialization': 'sft_native_chat_v1',
                          'lora.r': 64, 'lora.lora_alpha': 128, 'training.learning_rate': .00005,
                          'context.max_total_tokens': 4096, 'training.micro_batch_tuning': 'auto',
                          'data.train_slice_start': 0.0, 'data.train_slice_end': 1.0}}


def _source_defaults(manifest, overrides, *, stage, purpose):
    result = dict(overrides or {})
    if manifest.get('coverage_policy', {}).get('schema') != 'mined-corpus/1' or stage != 1:
        return result
    shares = {task: 0.0 for task in (*rs.cfgmod.TASKS, 'regression_test_design',
                                    'upstream_patch', 'code_completion', 'change_analysis')}
    shares.update(upstream_patch=.55, code_completion=.30, change_analysis=.15)
    defaults = {'model.serialization': 'sft_native_chat_v1', 'sampling.task_shares': shares,
                'data.train_slice_start': 0.0, 'data.train_slice_end': 1.0}
    if purpose == 'train' and result.get('precision.mode', 'qlora_nf4') == 'qlora_nf4':
        defaults.update({'training.micro_batch_tuning': 'auto', 'training.length_grouping': True,
                         'training.lm_loss_chunk_tokens': 512})
    for key, value in defaults.items():
        result.setdefault(key, value)
    return result

@guarded
def prepare(*, dataset_dir, run_dir, stage: int = 1, steps: int | None = None, eval_max: int | None = None, purpose: str = "train",
            session_minutes: float | None = None, loss_mode: str | None = None, eval_views: int | None = None, api=None, revision: str | None = None, tokenizer_revision: str | None = None, source_root=None,
            overrides: dict | None = None, profile: str | None = None, mode: str = 'full', total_budget_minutes: float = 30) -> dict:
    """Resolve the run (pinning the base model and tokenizer commits), export its bundle and notebook, and record what was made."""
    dataset_dir, run_dir = pathlib.Path(dataset_dir), pathlib.Path(run_dir)
    mode, total_budget_minutes = _trial_options(mode, total_budget_minutes)
    check_run_name(run_dir.name)
    source_dataset = None
    if mode == 'trial':
        import hashlib
        from . import trial_data
        source_manifest_bytes = (dataset_dir / 'manifest.json').read_bytes()
        source_manifest = json.loads(source_manifest_bytes)
        already_derived = bool(source_manifest.get('trial_version'))
        if already_derived:
            parent = source_manifest.get('source') or {}
            source_dataset = {key: parent.get(key) for key in ('tag', 'manifest_sha256')}
            if (not isinstance(source_dataset['tag'], str) or not re.fullmatch(r'[0-9a-f]{64}', str(source_dataset['manifest_sha256']))
                    or not (source_manifest.get('coverage_policy') or {}).get('trial_policy', '').startswith('portable-source-trial/')
                    or (source_manifest.get('evaluation') or {}).get('security_evaluation') is not False):
                raise ValueError('The existing derivative is not an authenticated source trial')
            derived, manifest = dataset_dir, source_manifest
        else:
            source_dataset = {'tag': source_manifest.get('tag'),
                              'manifest_sha256': hashlib.sha256(source_manifest_bytes).hexdigest()}
            run_dir.mkdir(parents=True, exist_ok=True)
            derived = run_dir / 'trial_data'
            if not derived.exists():
                trial_data.build(dataset_dir, derived, seed=17, train_views=256, eval_views=24, target_families=64)
            manifest = util.read_json(derived / 'manifest.json') or {}
        from .finetune_import import _sha_file
        selection = manifest.get('selection') or {}
        expected = {'parent_manifest_sha256': source_dataset['manifest_sha256'], 'seed': 17,
                    'train_views': 256, 'eval_views': 24, 'target_families': 64}
        if any(selection.get(key) != value for key, value in expected.items()):
            raise ValueError('The existing trial data belongs to different source inputs or selection settings')
        files = manifest.get('files') or {}
        if not {'records.parquet', 'views.parquet', 'instruction_views.jsonl'}.issubset(files):
            raise ValueError('The existing trial data is missing its immutable file inventory')
        for name, item in files.items():
            if (not re.fullmatch(r'[A-Za-z0-9_.-]+', name) or not (derived / name).is_file()
                    or (derived / name).is_symlink() or _sha_file(derived / name) != item.get('sha256')
                    or (derived / name).stat().st_size != item.get('bytes')):
                raise ValueError('The immutable trial dataset changed: ' + name)
        dataset_dir = derived
    if (run_dir / rs.FILE).exists():
        previous_resolved = json.loads((run_dir / rs.FILE).read_text(encoding='utf-8'))
        if overrides is None:
            overrides = previous_resolved.get('overrides')
        revision = revision or previous_resolved['pinned']['model_revision']
        tokenizer_revision = tokenizer_revision or previous_resolved['pinned']['tokenizer_revision']
    if not (run_dir / rs.FILE).exists():
        if mode == 'trial':
            recipe = trial_profile()
            supplied = dict(overrides or {})
            overrides = {**recipe['overrides'], **supplied}
            if overrides['model.id'] == recipe['overrides']['model.id']:
                revision = revision or recipe['revision']
                tokenizer_revision = tokenizer_revision or recipe['tokenizer_revision']
            profile = profile or ('custom' if any(recipe['overrides'].get(key) != value for key, value in supplied.items()) else 'portable_trial')
        overrides = _source_defaults(json.loads((dataset_dir / 'manifest.json').read_text(encoding='utf-8')),
                                     overrides, stage=stage, purpose=purpose)
        selected_model = (overrides or {}).get('model.id') or rs.cfgmod.load().model.id
        if 'instruct' in selected_model.lower():
            overrides = {**(overrides or {}), 'model.serialization': (overrides or {}).get('model.serialization', 'native_chat_v1')}
    cfg = rs.resolve(dataset_dir=dataset_dir, run_dir=run_dir, api=api, revision=revision, tokenizer_revision=tokenizer_revision, overrides=overrides)
    from app.backend.pipeline import finetune_catalog
    run_id = finetune_catalog.run_identity(run_dir.parent, run_dir.name)
    from training.finetune import model_eligibility
    model_eligibility.validate(cfg.model.id, cfg.model.revision, api=api)
    summary = bd.export_bundle(dataset_dir=dataset_dir, run_dir=run_dir, out_path=run_dir / "bundle.zip", source_root=source_root)
    tpu = cfg.precision.mode == "tpu_int4_bf16"
    requirements_path = REQUIREMENTS.with_name("requirements-finetune-tpu.txt") if tpu else REQUIREMENTS
    requirements = requirements_path.read_text(encoding="utf-8").splitlines()
    notebook = ng.write(run_dir / "finetune.ipynb", bundle_sha256=summary["sha256"], requirements=requirements, stage=stage, steps=steps,
                        bundle_zip_bytes=(run_dir / "bundle.zip").stat().st_size,
                        eval_max=eval_max, purpose=purpose, session_minutes=session_minutes,
                        loss_mode=loss_mode, eval_views=eval_views, device="tpu" if tpu else "auto", parent_run_id=run_id,
                        mode=mode, total_budget_minutes=total_budget_minutes)
    previous = util.read_json(run_dir / PREPARED) or {}
    info = {"schema": "finetune-prepared/1", "run": run_dir.name, "run_id": run_id, "state": "configured", "model": cfg.model.id,
            "mode": mode, "total_budget_minutes": total_budget_minutes,
            "model_revision": cfg.model.revision, "tokenizer_revision": cfg.model.tokenizer_revision, "stage": stage, "steps": steps,
            "purpose": purpose, "session_minutes": session_minutes, "loss_mode": loss_mode, "eval_views": eval_views, "eval_max": eval_max, "dataset": {"tag": cfg.data.dataset_version, "manifest_sha256": cfg.data.manifest_sha256,
                                              "status": json.loads((dataset_dir / "manifest.json").read_text(encoding="utf-8"))
                                              .get("dataset_status", "unfrozen")},
            "bundle": {"path": "bundle.zip", "sha256": summary["sha256"], "bytes": summary["bytes"], "files": summary["files"],
                       "warnings": summary["warnings"]},
            "notebook": {"path": "finetune.ipynb", "sha256": notebook["sha256"]},
            "prepared_at": previous.get("prepared_at") or util.now_utc()}
    if source_dataset:
        info.update(source_dataset=source_dataset, experimental=True, security_evaluation=False, counts_as_full_training=False,
                    trial_data=json.loads((dataset_dir / 'manifest.json').read_text(encoding='utf-8')))
    from training.finetune import quality_gate
    info['training_gate'] = quality_gate.check(dataset_dir, purpose=purpose,
        config_sha256=json.loads((run_dir / rs.FILE).read_text(encoding='utf-8')).get('config_sha256'))
    from app.backend.pipeline import finetune_eval_import
    info['evaluation_availability'] = finetune_eval_import.availability(run_dir)
    if overrides:
        info["overrides"] = dict(overrides)
        info["profile"] = profile or "custom"
    _write_json(run_dir / PREPARED, info)
    return info


# -- the CPU check ---------------------------------------------------------------------------------------------------------------------------------


@guarded
def refresh_notebook_workspace(*, run_dir) -> pathlib.Path:
    """Refresh legacy notebook paths without rebuilding the bundle; archive the exact original."""
    import hashlib
    from app.backend.pipeline.oslock import FileLock
    run_dir = pathlib.Path(run_dir)
    path = run_dir / 'finetune.ipynb'
    with FileLock(run_dir / '.notebook-workspace.lock', note=False):
        prepared = util.read_json(run_dir / PREPARED)
        if not prepared:
            raise ValueError('Prepare this run before downloading its notebook')
        original = path.read_bytes()
        digest = hashlib.sha256(original).hexdigest()
        notebook = json.loads(original)
        updated = ng.upgrade_workspace(notebook, prepared['bundle']['sha256'])
        journal_path = run_dir / '.notebook_workspace_upgrade.json'
        if updated is notebook:
            if prepared['notebook']['sha256'] != digest:
                journal = util.read_json(journal_path) or {}
                if (journal.get('from_sha256') != prepared['notebook']['sha256']
                        or journal.get('to_sha256') != digest):
                    raise ValueError('The saved notebook changed outside its recorded workspace upgrade')
                prepared['notebook'] = {**prepared['notebook'], 'sha256': digest}
                _write_json(run_dir / PREPARED, prepared)
            return path
        if prepared['notebook']['sha256'] != digest:
            raise ValueError('The saved notebook changed after preparation')
        history = run_dir / '.notebook_history'
        history.mkdir(exist_ok=True)
        archived = history / (digest + '.ipynb')
        if not archived.exists():
            archived.write_bytes(original)
        content = (json.dumps(updated, indent=1, ensure_ascii=False) + '\n').encode('utf-8')
        new_digest = hashlib.sha256(content).hexdigest()
        _write_json(journal_path, {'from_sha256': digest, 'to_sha256': new_digest})
        temporary = path.with_name('.finetune-workspace.ipynb.tmp')
        temporary.write_bytes(content)
        os.replace(temporary, path)
        prepared['notebook'] = {**prepared['notebook'], 'sha256': new_digest}
        _write_json(run_dir / PREPARED, prepared)
    return path

@guarded
def validate(*, run_dir, steps: int = 2, stage: int = 2, eval_max: int | None = 16, timeout: int = 3600, python: str | None = None) -> dict:
    """Unpack the bundle in an empty folder and run the runner on a tiny random model there; the report is written to cpu_validation.json."""
    run_dir = pathlib.Path(run_dir)
    prepared = util.read_json(run_dir / PREPARED)
    if not prepared:
        raise FileNotFoundError(f"{run_dir / PREPARED} does not exist: prepare the run first")
    bundle = run_dir / "bundle.zip"
    problems = bd.verify_bundle(bundle)
    if problems:
        raise bd.BundleError("the bundle does not verify: " + "; ".join(problems))
    sha = prepared["bundle"]["sha256"]
    scratch = run_dir / "validation"
    if scratch.exists():
        shutil.rmtree(scratch)
    work, out = scratch / "bundle", scratch / "run"
    bd.unpack_bundle(bundle, work)
    python = python or sys.executable
    env = {**os.environ, "PYTHONPATH": str(work.resolve()), "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    # Tiny CPU diagnostics spend more time scheduling than computing with the
    # machine's full thread count. This never changes real training settings.
    env.setdefault('OMP_NUM_THREADS', '2')
    env.setdefault('MKL_NUM_THREADS', '2')
    probe = compute.run_text([python, "-c", "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('app') is None else 1)"],
                             cwd=work, env=env, timeout=120)
    command = [python, "-m", "training.finetune.runner", "--bundle", str(work.resolve()), "--run-dir", str(out.resolve()), "--tiny",
               "--steps", str(steps), "--stage", str(stage)] + (["--eval-max", str(eval_max)] if eval_max else [])
    started = time.monotonic()
    proc = compute.run_text(command, cwd=work, env=env, timeout=timeout)
    seconds = time.monotonic() - started
    config = util.read_json(out / "run_config.json") or {}
    output = (proc.stdout + proc.stderr).strip().splitlines()
    passed = (proc.returncode == 0 and probe.returncode == 0 and config.get("state") == "finished" and config.get("smoke_test") is True
              and (out / "outputs.zip").exists())
    report = {"schema": "cpu-validation/1", "passed": passed, "returncode": proc.returncode, "bundle_sha256": sha, "steps": steps,
              "stage": stage, "seconds": round(seconds, 1), "smoke_test": config.get("smoke_test"), "run_state": config.get("state"),
              "app_importable": probe.returncode != 0, "environment": config.get("environment"),
              "output_tail": "\n".join(output[-20:]), "note": CPU_NOTE, "checked_at": util.now_utc()}
    _write_json(run_dir / VALIDATION, report)
    return report


# -- state ---------------------------------------------------------------------------------------------------------------------------------------------

STATE_ORDER = ("configured", "cpu_validated", "gpu_smoke_tested", "trained", "evaluated")


def _current_bundle_manifest_sha(run_dir: pathlib.Path):
    import hashlib
    import zipfile
    try:
        with zipfile.ZipFile(run_dir / "bundle.zip") as z:
            return hashlib.sha256(z.read("bundle_manifest.json")).hexdigest()
    except (OSError, KeyError, zipfile.BadZipFile):
        return None


def _evaluations_of(adapter_sha: str, experiments: pathlib.Path) -> list[str]:
    """Tags of evaluation runs (a run_config naming this adapter and a metrics.json beside it)."""
    found = []
    for d in sorted(p for p in pathlib.Path(experiments).iterdir() if p.is_dir()) if pathlib.Path(experiments).exists() else []:
        cfg = util.read_json(d / "run_config.json")
        if (isinstance(cfg, dict) and cfg.get("adapter_sha256") == adapter_sha and (d / "metrics.json").exists()
                and cfg.get('mode') != 'trial' and not cfg.get('experimental') and cfg.get('security_evaluation') is not False
                and not cfg.get("smoke_test") and not cfg.get("partial")
                and cfg.get('counts_as_evaluation') is not False and not cfg.get('excluded')):
            found.append(d.name)
    return found


def run_state(run_dir, experiments=pathlib.Path("experiments")) -> dict:
    """How far a run has got, from evidence on disk; the states it has not reached are listed with what they need.

    configured: a verified bundle for the resolved run. cpu_validated: a passed CPU check of that very bundle. gpu_smoke_tested: an imported
    short GPU run of that bundle. trained: an imported finished run of that bundle. evaluated: an evaluation run (with metrics) of the
    trained adapter. The state is the highest one reached; an import of another bundle counts for nothing."""
    run_dir = pathlib.Path(run_dir)
    prepared = util.read_json(run_dir / PREPARED)
    if not prepared:
        return {"state": "not_prepared", "evidence": {}, "blocked": [{"state": "configured", "reason": "prepare the run first"}]}
    bundle = run_dir / "bundle.zip"
    verified = False
    if bundle.exists():
        try:
            import hashlib
            verified = hashlib.sha256(bundle.read_bytes()).hexdigest() == prepared["bundle"]["sha256"] and not bd.verify_bundle(bundle)
            rs.read_resolved(run_dir, verify_contract=False)
        except Exception:  # ignored: any failure means the bundle is not verified (verified = False below)
            verified = False
    validation = util.read_json(run_dir / VALIDATION)
    validated = bool(verified and validation and validation.get("passed") is True
                     and validation.get("bundle_sha256") == prepared["bundle"]["sha256"])
    from app.backend.pipeline import finetune_import as fi
    manifest_sha = _current_bundle_manifest_sha(run_dir) if verified else None
    imports = [i for i in fi.list_imports(run_dir) if prepared.get('mode') != 'trial'
               and manifest_sha and i.get("bundle_manifest_sha256") == manifest_sha]
    trained = [i for i in imports if i["type"] == "trained"]
    evaluations = sorted({t for i in trained for t in _evaluations_of(i["adapter"]["sha256"], experiments)})
    reached = {"configured": verified, "cpu_validated": validated,
               "gpu_smoke_tested": any(i["type"] in ("gpu_smoke", "tpu_smoke") for i in imports), "trained": bool(trained), "evaluated": bool(evaluations)}
    state = next((k for k in reversed(STATE_ORDER) if reached[k]), "not_prepared")
    reasons = {"configured": "the bundle is missing, was edited, or no longer matches the resolved run",
               "cpu_validated": "run the CPU check on this bundle" + ("" if not validation else (
                   " (the last check failed)" if not validation.get("passed") else " (the last check was of another bundle)")),
               **BLOCKED}
    blocked = [{"state": k, "reason": reasons[k]} for k in STATE_ORDER if not reached[k]]
    return {"state": state, "blocked": blocked, "evidence": {
        "bundle_verified": verified,
        "cpu_validation": validation and {"passed": validation.get("passed"), "bundle_sha256": validation.get("bundle_sha256"),
                                          "checked_at": validation.get("checked_at")},
        "imports": [{"import_id": i["import_id"], "type": i["type"], "state": i["run"]["state"], "stage": i["run"].get("stage"),
                     "loss_mode": i["run"].get("loss_mode"), "invariance": i["run"].get("invariance", False),
                     "adapter_sha256": i["adapter"]["sha256"], "imported_at": i["imported_at"]} for i in imports],
        "evaluations": evaluations}}


def list_runs(root=RUNS) -> list[dict]:
    root = pathlib.Path(root)
    out = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []:
        prepared = util.read_json(d / PREPARED)
        if not prepared:
            continue
        s = run_state(d)
        out.append({"name": d.name, "state": s["state"], "dataset": prepared["dataset"], "model_revision": prepared["model_revision"],
                    "stage": prepared["stage"], "bundle": prepared["bundle"], "prepared_at": prepared.get("prepared_at"),
                    "blocked": s["blocked"], "model": prepared.get("model"), "purpose": prepared.get("purpose"),
                    "profile": prepared.get("profile"), "overrides": len(prepared.get("overrides") or {})})
    return out


def reexport_params(prepared: dict) -> dict:
    """The parameters of the ``export_training_bundle`` job that prepare the same run again under another name: the same dataset version, stage, purpose,
    budget, overrides and profile, and the same two pinned commits (so the Hub is not asked again and the base model cannot move). Left out when not set."""
    keys = {"tag": (prepared.get('source_dataset') or prepared.get("dataset") or {}).get("tag"), "stage": prepared.get("stage"), "purpose": prepared.get("purpose"), "refresh": True,
            "steps": prepared.get("steps"), "eval_max": prepared.get("eval_max"), "eval_views": prepared.get("eval_views"),
            "session_minutes": prepared.get("session_minutes"), "loss_mode": prepared.get("loss_mode"), "overrides": prepared.get("overrides"),
            "profile": prepared.get("profile"), "revision": prepared.get("model_revision"), "tokenizer_revision": prepared.get("tokenizer_revision")}
    if prepared.get('mode') == 'trial':
        keys.update(mode=prepared['mode'], total_budget_minutes=prepared.get('total_budget_minutes', 30))
    return {k: v for k, v in keys.items() if v not in (None, "", {})}
