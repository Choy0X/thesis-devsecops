"""Importing an evaluation (``eval_outputs.zip``, written by ``training/finetune/runner.py --evaluate``). No torch.

An evaluation is a model's answers (attempts) on the validation views. It is checked before it is scored; one failed check rejects the whole
import and keeps nothing:

* ``zip_structure`` and ``manifest``: no path leaving the folder, every file against its manifest, the required files present;
* ``evaluation_config``, ``identity`` and ``phase``: a finetune-eval config for the validation split, the prepared run's pinned commits,
  dataset manifest, resolved config, bundle and serializer, RAG off with zero calls;
* ``generation``: greedy (no sampling) and, unless it says it ran on a smoke model, the contract's token limits;
* ``adapter``: for a trained control, an adapter that was imported as a trained run of this very bundle and that is the control the baseline
  names (B2 and B3 stage one with the ordinary and the field-balanced loss, B4 stage two, B5 with the invariance loss); B0 takes no adapter;
* ``attempts``, ``coverage`` and ``truth``: valid attempts for the shared evaluator, no duplicates, every view the evaluation was asked about
  attempted or excluded with a reason (a missing one is a rejection, never a smaller denominator), and every truth and every fact the results
  are broken down by equal what the bundle's own dataset says. A repair attempt carries no verification: no receipt exists for a model patch.

An accepted evaluation is scored by the shared evaluator into a new run folder under ``experiments/`` (``metrics.json``, ``attempts.parquet``,
``run_config.json`` naming the baseline and the adapter hash). It makes a control *evaluated* only when it is complete and did not run on a
smoke model; a partial or software evaluation is kept as evidence and never counts (see ``baselines`` and ``finetune_prep.run_state``).
"""
from __future__ import annotations
from app.backend.pipeline.finetune_deletion import guarded

import hashlib
import io
import json
import pathlib
import shutil
import zipfile
from collections import Counter

from training.finetune import attempts as at

from app.backend.pipeline import baselines as bl, finetune_import as fi, finetune_prep as fp
from app.backend.pipeline.eval import shared
from app.backend.pipeline import util

SCHEMA = "finetune-eval-import/1"
EVAL_SCHEMA = "finetune-eval/1"
OUTPUTS_SCHEMA = "eval-outputs/1"
MANIFEST = "eval_outputs_manifest.json"
REQUIRED = ("evaluation_config.json", "attempts.parquet", "resolved_config.json")
CHECKS = ("zip_structure", "manifest", "evaluation_config", "identity", "generation", "baseline", "adapter", "evaluation_dataset",
          "attempts", "coverage", "truth", "facts")
TASKS = ("discovery", "repair")
ROLES = ("selection", "calibration")
EXCLUSIONS = ("prompt_over_budget",)
TASK_TRACKS = {"discovery": ("generated_decision",), "repair": ("repair",)}


class EvalRejected(ValueError):
    """The evaluation failed one or more checks; ``failures`` lists them and nothing was kept."""

    def __init__(self, failures: list[dict]):
        self.failures = failures
        super().__init__("; ".join(f"{f['check']}: {f['detail']}" for f in failures))


def availability(run_dir) -> dict:
    """Cheap display diagnostic. Full benchmark preflight remains mandatory.

    Read only the small publication manifests; listing never reads model weights
    or materializes the training tables.
    """
    try:
        with zipfile.ZipFile(pathlib.Path(run_dir) / 'bundle.zip') as archive:
            training = json.loads(archive.read('data/manifest.json'))
            bundle = json.loads(archive.read('bundle_manifest.json'))
        if bundle.get('evaluation_dataset'):
            return {'state': 'not_checked', 'reason_code': 'benchmark_preflight_required',
                    'detail': 'An independent benchmark is packaged. Labels, partition role and overlap still require preflight.',
                    'remediation': 'Run evaluation with the pinned benchmark; preflight runs before model loading.',
                    'evaluation_dataset': bundle['evaluation_dataset']}
        if training.get('coverage_policy', {}).get('schema') == 'mined-corpus/1':
            return {'state': 'blocked', 'reason_code': 'unsupported_corpus',
                    'detail': 'The mined source-learning corpus supplies no held-out reviewed security evaluation cohort.',
                    'remediation': 'Supply a frozen independent benchmark and its manifest SHA256. Preserve the original training dataset.'}
        return {'state': 'not_checked', 'reason_code': 'evaluation_preflight_required',
                'detail': 'Security evaluation eligibility has not been checked.',
                'remediation': 'Run the data preflight before loading model weights.'}
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        return {'state': 'blocked', 'reason_code': 'missing_benchmark',
                'detail': 'Evaluation publication metadata is unavailable: ' + str(error),
                'remediation': 'Restore the original verified training bundle and provide the benchmark.'}


def list_imports(run_dir) -> list[dict]:
    root = pathlib.Path(run_dir) / "eval_imports"
    out = []
    for d in sorted(root.iterdir()) if root.exists() else []:
        try:
            out.append(json.loads((d / "import.json").read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda r: (r.get("imported_at", ""), r.get("import_id", "")))


def _fail(check: str, detail: str) -> dict:
    return {"check": check, "detail": detail}


def _manifest(folder: pathlib.Path, names: set[str]) -> list[dict]:
    manifest = util.read_json(folder / MANIFEST)
    if not isinstance(manifest, dict) or manifest.get("schema") != OUTPUTS_SCHEMA or not isinstance(manifest.get("files"), dict):
        return [_fail("manifest", f"{MANIFEST} is missing or is not a {OUTPUTS_SCHEMA} file")]
    listed, out = manifest["files"], []
    for name, info in sorted(listed.items()):
        if name not in names:
            out.append(_fail("manifest", f"{name} is missing from the artifact"))
        elif fi._sha_file(folder / name) != info.get("sha256") or (folder / name).stat().st_size != info.get("bytes"):
            out.append(_fail("manifest", f"{name} changed after the evaluation wrote it"))
    out += [_fail("manifest", f"{n} is in the artifact but not in its manifest") for n in sorted(names - set(listed) - {MANIFEST})]
    out += [_fail("manifest", f"the required file {n} is not listed in the manifest") for n in REQUIRED if n not in listed]
    return out


def _bundle_data(run_dir: pathlib.Path) -> tuple[list[dict], dict[str, dict]]:
    """The dataset the bundle carries: its views and its records (by record id)."""
    import pyarrow.parquet as pq
    with zipfile.ZipFile(run_dir / "bundle.zip") as z:
        views = pq.read_table(io.BytesIO(z.read("data/views.parquet"))).to_pylist()
        have = set(pq.ParquetFile(io.BytesIO(z.read("data/records.parquet"))).schema_arrow.names)
        columns = [c for c in ("record_id", "repository_identity", "ecosystem", "language", "severity_label", "file_path_after") if c in have]
        records = pq.read_table(io.BytesIO(z.read("data/records.parquet")), columns=columns).to_pylist()
    return views, {r["record_id"]: r for r in records}


def _independent(cfg):
    value = cfg.get('evaluation_data')
    return isinstance(value, dict) and value.get('independent') is True


def _evaluation_data(cfg, run_dir, folder, exp, evaluation_dataset_dir=None):
    """Recheck against trusted data, never against an imported audit assertion.

    Independent benchmarks may be explicitly supplied or carried by the prepared
    bundle. Its original data remains the adapter's training source throughout.
    """
    if not _independent(cfg):
        views, records = _bundle_data(run_dir)
        return views, records, None
    from training.finetune import evaluation_data as ed
    import pyarrow.parquet as pq
    scratch = folder / '_evaluation_sources'
    train = scratch / 'training'
    train.mkdir(parents=True)
    with zipfile.ZipFile(run_dir / 'bundle.zip') as archive:
        manifest = json.loads(archive.read('bundle_manifest.json'))
        def copy_member(name, target):
            info = manifest.get('files', {}).get(name)
            if not info or name not in archive.namelist():
                raise EvalRejected([_fail('evaluation_dataset', 'Missing authenticated source member: ' + name)])
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(name) as source, target.open('wb') as destination:
                shutil.copyfileobj(source, destination)
            if ed.sha_file(target) != info.get('sha256') or target.stat().st_size != info.get('bytes'):
                raise EvalRejected([_fail('evaluation_dataset', 'Prepared bundle source member changed: ' + name)])
        for name in ('manifest.json', 'records.parquet', 'views.parquet'):
            copy_member('data/' + name, train / name)
        if evaluation_dataset_dir is not None:
            evaluation = pathlib.Path(evaluation_dataset_dir)
        else:
            packaged = manifest.get('evaluation_dataset') or {}
            if packaged.get('data_dir') != 'eval_data':
                raise EvalRejected([_fail('evaluation_dataset',
                    'Independent evaluation needs the reviewed benchmark directory (evaluation_dataset_dir), or packaged eval_data. The result cannot certify its own benchmark.')])
            evaluation = scratch / 'benchmark'
            copy_member('eval_data/manifest.json', evaluation / 'manifest.json')
            if ed.sha_file(evaluation / 'manifest.json') != packaged.get('manifest_sha256'):
                raise EvalRejected([_fail('evaluation_dataset', 'Packaged benchmark manifest identity changed')])
            benchmark = json.loads((evaluation / 'manifest.json').read_text(encoding='utf-8'))
            for name in benchmark.get('files', {}):
                rel = pathlib.PurePosixPath(name)
                if rel.is_absolute() or '..' in rel.parts or '\\' in name or ':' in name or not rel.parts:
                    raise EvalRejected([_fail('evaluation_dataset', 'Unsafe benchmark path: ' + name)])
                copy_member('eval_data/' + name, evaluation / name)
    try:
        ready = ed.preflight(train, exp['cfg'], evaluation,
            (cfg.get('evaluation_dataset') or {}).get('manifest_sha256'),
            split=cfg['split'], role=cfg['role'], tasks=tuple(cfg['tasks']))
    except ed.EvaluationBlocked as error:
        raise EvalRejected([_fail('evaluation_dataset', f'{error.code}: {error.detail}')]) from error
    if ready['evaluation_dataset'] != cfg.get('evaluation_dataset') or ready['training_dataset'] != cfg.get('training_dataset'):
        raise EvalRejected([_fail('evaluation_dataset', 'Declared training/evaluation dataset identities differ from authenticated sources')])
    views = pq.read_table(evaluation / 'views.parquet').to_pylist()
    records = pq.read_table(evaluation / 'records.parquet').to_pylist()
    checked = {key: value for key, value in ready.items() if key not in ('manifest', 'views')}
    checked['_source_directories'] = (train, evaluation)
    return views, {row['record_id']: row for row in records}, checked


def _config_checks(cfg: dict, exp: dict) -> list[dict]:
    out = []
    if cfg.get("schema") != EVAL_SCHEMA or cfg.get("state") != "finished":
        out.append(_fail("evaluation_config", f"evaluation_config.json is not a finished {EVAL_SCHEMA} config"))
    if (cfg.get('split'), cfg.get('role')) not in (('val', 'selection'), ('val', 'calibration'), ('test', 'untouched_test')):
        out.append(_fail("evaluation_config", f"an evaluation answers the validation split with the {ROLES} role, got {cfg.get('split')!r} / {cfg.get('role')!r}"))
    if not cfg.get("tasks") or not set(cfg["tasks"]) <= set(TASKS):
        out.append(_fail("evaluation_config", f"tasks must be a subset of {TASKS}, got {cfg.get('tasks')!r}"))
    serialization = cfg.get('serialization') or {}
    seen = {**cfg, "prompt_text": {"serialization_hash": serialization.get("hash"),
            "serialization": serialization.get('id'), "chat_template": serialization.get('chat_template')}}
    if _independent(cfg):
        if not isinstance(cfg.get('training_dataset'), dict) or not isinstance(cfg.get('evaluation_dataset'), dict):
            out.append(_fail('identity', 'Independent evaluation requires separate training_dataset and evaluation_dataset identities'))
        if cfg.get('dataset') != cfg.get('evaluation_dataset'):
            out.append(_fail('identity', 'The dataset alias must identify evaluation_dataset'))
        seen['dataset'] = cfg.get('training_dataset')
    else:
        for field in ('training_dataset', 'evaluation_dataset'):
            if field in cfg and cfg[field] != cfg.get('dataset'):
                out.append(_fail('identity', f'{field} differs from the declared same-dataset evaluation identity'))
    out += fi._identity(seen, exp)
    gen = cfg.get("generation") or {}
    if gen.get("do_sample") is not False:
        out.append(_fail("generation", "generation must be greedy (do_sample false)"))
    ev = exp["cfg"].evaluation
    contract = {"discovery": ev.greedy_max_new_tokens, "repair": ev.repair_max_new_tokens}
    if not cfg.get("smoke_test") and gen.get("max_new_tokens") != contract:
        out.append(_fail("generation", f"max_new_tokens is {gen.get('max_new_tokens')!r}; a run that is not a smoke test uses the contract's {contract}"))
    return out


def _adapter_checks(cfg: dict, run_dir: pathlib.Path, experiments: pathlib.Path, exp: dict) -> list[dict]:
    baseline, adapter = cfg.get("baseline"), cfg.get("adapter")
    if baseline not in ("B0", "B2", "B3", "B4", "B5"):
        return [_fail("baseline", f"baseline {baseline!r} cannot be imported: B1 is another model and B6 has no stage; the others are B0, B2, B3, B4, B5")]
    if baseline == "B0":
        if adapter is not None:
            return [_fail("baseline", "B0 is the untouched base: it takes no adapter")]
        if cfg.get("model") != exp["prepared"]["model"]:
            return [_fail("baseline", "B0 must be the pinned base model")]
        return []
    if not isinstance(adapter, dict):
        return [_fail("adapter", f"{baseline} is a trained control: the evaluation names no adapter")]
    trained = [i for i in fp.run_state(run_dir, experiments=experiments)["evidence"].get("imports", []) if i["type"] == "trained"]
    match = [i for i in trained if i["adapter_sha256"] == adapter.get("sha256")]
    if not match:
        return [_fail("adapter", f"the adapter {str(adapter.get('sha256'))[:12]} was not imported as a trained run of this bundle "
                                 f"({len(trained)} trained import(s) exist)")]
    if adapter.get("bundle_manifest_sha256") != exp["bundle_manifest_sha256"]:
        return [_fail("adapter", "the adapter was trained from another bundle than this run's")]
    if not any(bl._is(baseline, i) for i in match):
        i = match[0]
        return [_fail("adapter", f"{baseline} is not what that adapter was trained as (stage {i.get('stage')}, loss {i.get('loss_mode')}, "
                                 f"invariance {i.get('invariance')})")]
    return []


def _attempt_checks(cfg: dict, rows: list[dict], views: list[dict], records: dict) -> list[dict]:
    out: list[dict] = []
    problems = [f"{r.get('attempt_id')}: {p}" for r in rows for p in at.validate_attempt(r)]
    if problems:
        out.append(_fail("attempts", "; ".join(problems[:5]) + (f" (+{len(problems) - 5} more)" if len(problems) > 5 else "")))
        return out
    arms = {r["arm"] for r in rows}
    if arms != {cfg.get("arm")}:
        out.append(_fail("attempts", f"the attempts name the arms {sorted(arms)}, the evaluation says {cfg.get('arm')!r}"))
    ids = [r["attempt_id"] for r in rows]
    keys = [(r["view_id"], r["track"]) for r in rows]
    if len(set(ids)) != len(ids) or len(set(keys)) != len(keys):
        out.append(_fail("attempts", "duplicate attempts (the same attempt id, or two attempts for one view and track)"))
    head = bool((cfg.get("adapter") or {}).get("head_trained"))
    allowed = {"generated_decision", "repair", *(["head_decision"] if head else [])}
    extra = sorted({r["track"] for r in rows} - allowed)
    if extra:
        out.append(_fail("attempts", f"attempts on the track(s) {extra}" + ("" if head else ": a head_decision attempt needs a trained decision head")))
    if any(r.get("split") != cfg.get('split') for r in rows):
        out.append(_fail("attempts", "every attempt must be on the declared evaluation split"))
    bad = [r["attempt_id"] for r in rows if r["track"] == "repair" and (r.get("verification") != "unavailable" or r.get("receipt_id"))]
    if bad:
        out.append(_fail("attempts", f"a model's repair has no execution receipt, so its verification is unavailable ({bad[0]})"))

    # coverage: the views the dataset says the evaluation was asked about
    pool = sorted((v for v in views if v["split"] == cfg['split'] and (cfg['split'] == 'test' or v["val_role"] == cfg["role"]) and v["task"] in cfg["tasks"]
                   and v["eligible"] and (v["weight"] or 0) > 0), key=lambda v: v["view_id"])
    expected_all = [v["view_id"] for v in pool]
    cap = cfg.get("max_views")
    expected = expected_all[:cap] if cap is not None else expected_all
    declared = (cfg.get("views") or {}).get("expected")
    if declared != expected:
        out.append(_fail("coverage", f"the evaluation lists {len(declared or [])} expected view(s) but the bundle's dataset has {len(expected)} for "
                                     f"the {cfg['role']} role and tasks {cfg['tasks']}"))
        return out
    if bool(cfg.get("partial")) != (cap is not None and cap < len(expected_all)):
        out.append(_fail("coverage", "partial must be true exactly when a cap left views out"))
    excluded = cfg.get("excluded") or {}
    if not set(excluded) <= set(expected) or set(excluded.values()) - set(EXCLUSIONS):
        out.append(_fail("coverage", f"excluded views must be expected views with a reason in {EXCLUSIONS}"))
    by_view: dict[str, set] = {}
    for r in rows:
        by_view.setdefault(r["view_id"], set()).add(r["track"])
    outside = sorted(set(by_view) - set(expected))
    if outside:
        out.append(_fail("coverage", f"view {outside[0]} is not in the validation {cfg['role']} role of the bundle's dataset"))
    by_id = {v["view_id"]: v for v in pool}
    for vid in expected:
        have = by_view.get(vid, set())
        if vid in excluded:
            if have:
                out.append(_fail("coverage", f"view {vid} is excluded and also attempted"))
            continue
        need = set(TASK_TRACKS[by_id[vid]["task"]]) | ({"head_decision"} if head and by_id[vid]["task"] == "discovery" else set())
        if need - have:
            out.append(_fail("coverage", f"view {vid} has no {sorted(need - have)[0]} attempt and is not excluded (an unattempted view would shrink the denominator)"))
            break
    if out:
        return out

    # truth and facts: what the dataset says, not what the file says
    for r in rows:
        v = by_id[r["view_id"]]
        want = at.facts_of(v, records.get(v["record_id"], {}))
        for key in at.FACT_KEYS:
            if r.get(key) != want[key]:
                out.append(_fail("truth" if key == "truth" else "facts",
                                 f"{key} of {r['attempt_id']} is {r.get(key)!r}, the dataset says {want[key]!r}"))
                return out
    return out


@guarded
def import_eval(zip_path, run_dir, *, experiments=pathlib.Path("experiments"), max_bytes: int = fi.MAX_BYTES, n_boot: int = 1000, dry_run: bool = False,
                evaluation_dataset_dir=None) -> dict:
    """Check an ``eval_outputs.zip`` against the prepared run and the bundle's dataset, score it and keep it. Raises ``EvalRejected``. With ``dry_run`` every check runs on a copy
    that is thrown away; nothing is scored and nothing is kept."""
    zip_path, run_dir, experiments = pathlib.Path(zip_path), pathlib.Path(run_dir), pathlib.Path(experiments)
    sha = fi._sha_file(zip_path)
    import_id = sha[:16]
    root = run_dir / "eval_imports"
    final, tmp = root / import_id, run_dir / (f".check-eval-{import_id}" if dry_run else f".tmp-eval-{import_id}")
    if (final / "import.json").exists():
        return {**json.loads((final / "import.json").read_text(encoding="utf-8")), "already_imported": True}
    exp = fi._expected_identity(run_dir)
    with zipfile.ZipFile(zip_path) as z:
        failures = fi._zip_structure(z, max_bytes)
        if failures:
            raise EvalRejected(failures)
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True)
        try:
            for info in z.infolist():
                if info.is_dir():
                    continue
                target = tmp / info.filename
                if not target.resolve().is_relative_to(tmp.resolve()):
                    raise EvalRejected([_fail("zip_structure", f"a path that would leave the import folder: {info.filename}")])
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            names = {p.relative_to(tmp).as_posix() for p in tmp.rglob("*") if p.is_file()}
            failures = _manifest(tmp, names)
            if failures:
                raise EvalRejected(failures)
            cfg = util.read_json(tmp / "evaluation_config.json")
            if not isinstance(cfg, dict):
                raise EvalRejected([_fail("evaluation_config", "evaluation_config.json does not parse")])
            failures = _config_checks(cfg, exp)
            if fi._sha_file(tmp / 'resolved_config.json') != exp['resolved_config_sha256']:
                failures.append(_fail('identity', 'The evaluation resolved_config.json bytes differ from the original training configuration'))
            if not failures:
                failures = _adapter_checks(cfg, run_dir, experiments, exp)
            if not failures:
                views, records, ready = _evaluation_data(cfg, run_dir, tmp, exp, evaluation_dataset_dir)
                source_directories = ready.pop('_source_directories') if ready else None
                rows = at.read_attempts(tmp / "attempts.parquet")
                failures = _attempt_checks(cfg, rows, views, records)
            if failures:
                raise EvalRejected(failures)
            c = exp["cfg"]
            tag = fp.check_run_name(run_dir.name) + f"-{cfg['baseline']}-{cfg['arm']}"
            counts = bool(not cfg.get("smoke_test") and not cfg.get("partial") and not cfg.get('excluded'))
            if dry_run:
                return {"schema": SCHEMA, "import_id": import_id, "baseline": cfg["baseline"], "arm": cfg["arm"], "role": cfg["role"], "tag": tag, "attempts": len(rows),
                        "excluded": len(cfg.get("excluded") or {}), "partial": bool(cfg.get("partial")), "smoke_test": bool(cfg.get("smoke_test")), "counts_as_evaluation": counts,
                        "checks": [{"name": n, "passed": True} for n in CHECKS], "already_imported": False, "dry_run": True}
            config = {"task": "evaluate_finetune", "baseline": cfg["baseline"], "arm": cfg["arm"], "adapter_sha256": (cfg.get("adapter") or {}).get("sha256"),
                      "model": cfg["model"], "model_revision": cfg["model_revision"], "split": cfg['split'], "role": cfg["role"], "tasks": cfg["tasks"], "n": len(rows),
                      "manifest_sha256": cfg["dataset"]["manifest_sha256"], "context_policy_hash": cfg["input_policy_hash"], "run": run_dir.name,
                      "eval_zip_sha256": sha, "bundle_manifest_sha256": exp["bundle_manifest_sha256"], "smoke_test": bool(cfg.get("smoke_test")),
                      "partial": bool(cfg.get("partial")), "excluded": len(cfg.get("excluded") or {}), "counts_as_evaluation": counts,
                      **({"external_data": cfg["external_data"]} if cfg.get("external_data") else {})}
            config['coverage_accounting'] = {'expected_views': len(cfg.get('views', {}).get('expected') or []),
                'attempted_views': len({row['view_id'] for row in rows}), 'excluded_views': len(cfg.get('excluded') or {}),
                'attempt_status_counts': dict(Counter(row['status'] for row in rows)),
                'raw_attempts_include_exclusions': False}
            config.update(tokenizer_revision=cfg.get('tokenizer_revision'), serialization=cfg.get('serialization'),
                generation=cfg.get('generation'), input_policy_hash=cfg.get('input_policy_hash'),
                context_policy=cfg.get('context_policy'),
                evaluation_cohort_sha256=hashlib.sha256(json.dumps({'split': cfg['split'], 'role': cfg['role'],
                    'tasks': sorted(cfg['tasks']), 'views': cfg['views']['expected']}, sort_keys=True).encode()).hexdigest())
            if cfg.get('training_dataset') is not None or cfg.get('evaluation_dataset') is not None:
                config.update(training_dataset=cfg.get('training_dataset'), evaluation_dataset=cfg.get('evaluation_dataset'),
                              evaluation_data=ready or cfg.get('evaluation_data'))
            security_report = None
            if source_directories is not None:
                from training.finetune import sft_evaluation
                training_data_dir, evaluation_data_dir = source_directories
                security_report = sft_evaluation.from_dataset(rows, evaluation_data_dir, training_data_dir=training_data_dir,
                    excluded=cfg.get('excluded') or {}, bootstrap_samples=n_boot)
            metric_attempts = list(rows)
            by_id = {view['view_id']: view for view in views}
            for vid, reason in (cfg.get('excluded') or {}).items():
                view = by_id[vid]
                tracks = list(TASK_TRACKS[view['task']])
                if view['task'] == 'discovery' and (cfg.get('adapter') or {}).get('head_trained'):
                    tracks.append('head_decision')
                for track in tracks:
                    metric_attempts.append({**at.facts_of(view, records.get(view['record_id'], {})),
                        'attempt_id': f'excluded:{cfg["arm"]}:{vid}:{track}', 'view_id': vid, 'arm': cfg['arm'],
                        'track': track, 'status': 'env_error', 'prediction': None,
                        'verification': 'unavailable' if track == 'repair' else None,
                        'detail': 'excluded_before_generation:' + reason})
            out = shared.write_run(rows, config, tag=tag, outdir=experiments, metric_attempts=metric_attempts,
                                   n_boot=n_boot, min_negatives=c.calibration.min_negatives,
                                   targets=tuple(c.evaluation.illustrative_fpr_operating_points))
            if security_report is not None:
                path = pathlib.Path(out['folder']) / 'metrics.json'
                metrics = json.loads(path.read_text(encoding='utf-8'))
                metrics['security_evaluation'] = security_report
                path.write_text(json.dumps(metrics, indent=1, sort_keys=True) + '\n', encoding='utf-8')
            record = {"schema": SCHEMA, "import_id": import_id, "zip_sha256": sha, "imported_at": util.now_utc(), "baseline": cfg["baseline"], "arm": cfg["arm"],
                      "role": cfg["role"], "adapter_sha256": config["adapter_sha256"], "tag": out["tag"], "folder": out["folder"],
                      "attempts": len(rows), "excluded": len(cfg.get("excluded") or {}), "partial": config["partial"], "smoke_test": config["smoke_test"],
                      "counts_as_evaluation": counts, "bundle_manifest_sha256": exp['bundle_manifest_sha256'],
                      "checks": [{"name": n, "passed": True} for n in CHECKS]}
            if cfg.get('training_dataset') is not None or cfg.get('evaluation_dataset') is not None:
                record.update(training_dataset=cfg.get('training_dataset'), evaluation_dataset=cfg.get('evaluation_dataset'),
                              evaluation_data=ready or cfg.get('evaluation_data'))
            final.mkdir(parents=True, exist_ok=True)
            (final / "import.json").write_text(json.dumps(record, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return {**record, "already_imported": False}
