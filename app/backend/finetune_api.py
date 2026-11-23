"""Routes that expose the fine-tune runs (``/api/finetune/runs``) to the UI.

Read-only: runs are prepared by the ``export_training_bundle`` job stage and checked by ``finetune_validate``. The state of a run is
derived from evidence files (see ``pipeline/finetune_prep.py``); an unexecuted notebook never makes a run "trained".
"""
from __future__ import annotations

import pathlib
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field, ConfigDict

from app.backend import errors
from fastapi.responses import FileResponse, JSONResponse
from app.backend.pipeline import util

router = APIRouter(prefix="/api/finetune")


@router.get('/runs/{name}/imports/{import_id}/exports')
def model_exports(name: str, import_id: str):
    from app.backend.pipeline import finetune_distribution
    folder, error = _run_dir(name)
    if error: return error
    try: return finetune_distribution.listing(folder, import_id)
    except (ValueError, OSError, KeyError) as problem: return _bad(400, str(problem))


@router.get('/runs/{name}/imports/{import_id}/exports/{filename}')
def download_model_export(name: str, import_id: str, filename: str):
    from app.backend.pipeline import finetune_distribution
    folder, error = _run_dir(name)
    if error: return error
    try: return FileResponse(finetune_distribution.artifact(folder, import_id, filename), filename=filename, media_type='application/zip')
    except (ValueError, OSError, KeyError) as problem: return _bad(400, str(problem))


def _bad(status: int, message: str) -> JSONResponse:
    return errors.error(message, status)


def _run_dir(name: str):
    from app.backend.pipeline import finetune_prep as fp
    try:
        fp.check_run_name(name)
    except ValueError:
        return None, _bad(400, "bad run name")
    d = pathlib.Path(fp.RUNS) / name
    if not (d / fp.PREPARED).exists():
        return None, _bad(404, "not found")
    return d, None


@router.get("/runs")
def runs(limit: Annotated[int, Query(ge=1, le=500)] = 100, offset: Annotated[int, Query(ge=0)] = 0, search: str = '', state: str = ''):
    from app.backend.pipeline import finetune_prep as fp
    from app.backend.pipeline import finetune_catalog
    return finetune_catalog.page(fp.RUNS, limit=limit, offset=offset, search=search, state=state)


@router.get('/trial-capability')
def trial_capability(tag: str):
    import re
    from app.backend.pipeline import trial_data, versions
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,100}', tag):
        return _bad(400, 'Invalid dataset version')
    return trial_data.capability(pathlib.Path(versions.PROC) / ('dataset_' + tag))


class DeletePreviewIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    names: list[str] | None = Field(default=None, max_length=100000)
    select_all: bool = False
    search: str = ''
    state: str = ''


class CreateRunIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,100}$')
    name: str | None = Field(default=None, max_length=64)
    tag: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,100}$')
    stage: Literal[1, 2] = 1
    mode: Literal['trial', 'full'] = 'full'
    total_budget_minutes: float = Field(default=30, gt=0, allow_inf_nan=False)
    purpose: Literal['train', 'smoke'] = 'train'
    steps: int | None = Field(default=None, gt=0)
    eval_max: int | None = Field(default=None, gt=0)
    eval_views: int | None = Field(default=None, gt=0)
    session_minutes: float | None = Field(default=None, gt=0)
    loss_mode: Literal['per_example_completion_ce', 'present_field_normalized_ce'] | None = None
    profile: str | None = None
    overrides: dict | None = None
    revision: str | None = Field(default=None, pattern=r'^[0-9a-f]{40}$')
    tokenizer_revision: str | None = Field(default=None, pattern=r'^[0-9a-f]{40}$')


@router.post('/runs')
def create_run(body: CreateRunIn):
    """New creation reserves an identity; existing run refresh/resume is separate."""
    from app.backend import jobs
    from app.backend.pipeline import finetune_catalog as catalog, finetune_prep as fp, versions
    from app.backend.pipeline.oslock import FileLock, Busy
    from training.finetune import quality_gate
    dataset = pathlib.Path(versions.PROC) / f'dataset_{body.tag}'
    if body.mode == 'trial':
        from app.backend.pipeline import trial_data
        assessment = trial_data.capability(dataset)
    else:
        assessment = quality_gate.data_preflight(dataset, purpose=body.purpose)
    if not assessment['allowed']:
        return JSONResponse({'error': {'message': 'Dataset is not ready for ' + ('a portable trial' if body.mode == 'trial' else 'full training')},
                             'training_assessment': assessment}, status_code=409)
    params = body.model_dump(exclude_none=True, exclude={'request_id', 'name'})
    try:
        reserved = catalog.reserve_run(fp.RUNS, request_id=body.request_id, name=body.name or None, params=params)
        with FileLock(pathlib.Path(fp.RUNS) / '.catalog' / f'create-{body.request_id}.lock', note=False):
            with catalog.connect(fp.RUNS) as conn:
                saved = conn.execute('SELECT job_id FROM reservations WHERE request_id=?', (body.request_id,)).fetchone()
            old = jobs.get(saved['job_id']) if saved and saved['job_id'] else None
            # Repeated requests return the same accepted work. Failed jobs may
            # be retried with the same reservation and immutable parent UUID.
            if old and old.status not in ('error', 'cancelled', 'interrupted'):
                return {**reserved, 'job_id': old.id}
            # Recover a crash between persisted job creation and catalog binding.
            if not old:
                recovered = next((row for row in jobs.listing() if row.get('stage') == 'export_training_bundle'
                    and (row.get('params') or {}).get('creation_request_id') == body.request_id), None)
                if recovered and recovered.get('status') not in ('error', 'cancelled', 'interrupted'):
                    with catalog.connect(fp.RUNS) as conn:
                        conn.execute('UPDATE reservations SET job_id=? WHERE request_id=?', (recovered['id'], body.request_id))
                    return {**reserved, 'job_id': recovered['id']}
            job = jobs.start('export_training_bundle', {**params, 'run': reserved['name'],
                'parent_run_id': reserved['run_id'], 'creation_request_id': body.request_id})
            with catalog.connect(fp.RUNS) as conn:
                conn.execute('UPDATE reservations SET job_id=? WHERE request_id=?', (job.id, body.request_id))
            return {**reserved, 'job_id': job.id}
    except (catalog.RunConflict, jobs.Blocked, jobs.TooManyJobs, Busy) as problem:
        return _bad(409, str(problem))
    except (ValueError, OSError) as problem:
        return _bad(422, str(problem))


class DeleteRunsIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    preview_token: str = Field(min_length=20, max_length=100)


@router.post('/runs/delete-preview')
def delete_preview(body: DeletePreviewIn):
    from app.backend.pipeline import finetune_prep as fp, finetune_deletion
    try:
        return finetune_deletion.preview(fp.RUNS, **body.model_dump())
    except (ValueError, OSError) as problem:
        return _bad(409 if isinstance(problem, finetune_deletion.Refused) else 400, str(problem))


@router.post('/runs/delete')
def delete_runs(body: DeleteRunsIn):
    from app.backend import jobs
    try:
        job = jobs.start('delete_finetune_runs', {'preview_token': body.preview_token})
        return {'job_id': job.id}
    except (jobs.Blocked, jobs.TooManyJobs, ValueError, OSError) as problem:
        return _bad(409, str(problem))


@router.get("/baselines")
def baselines():
    """B0 to B6: blocked, trained or evaluated, each with its reason; never a metric."""
    from app.backend.pipeline import baselines as bl, finetune_prep as fp
    return {"baselines": bl.records(fp.RUNS, bl.EXPERIMENTS)}


@router.get("/benchmarks")
def benchmarks():
    """The benchmark registry: every requested name, unresolved until a primary source is checked; never a score."""
    from training.finetune import benchmarks as bm
    reg = bm.load()
    return {"rules": reg.rules, "benchmarks": [{**b.model_dump(), "blocking_reason": bm.blocking_reason(b)} for b in reg.benchmarks]}


@router.get("/runs/{name}")
def run_detail(name: str):
    from app.backend.pipeline import finetune_eval_import as fe, finetune_import as fi, finetune_prep as fp
    d, err = _run_dir(name)
    if err:
        return err
    from app.backend.pipeline import gpu_local, finetune_catalog, versions, trial_import
    prepared = util.read_json(d / fp.PREPARED)
    from training.finetune.quality_gate import data_preflight
    import re
    dataset = (prepared or {}).get('dataset') or {}
    tag = ((prepared or {}).get('source_dataset') or dataset).get('tag') or ''
    assessment = data_preflight(pathlib.Path(versions.PROC) / ('dataset_' + tag), purpose=(prepared or {}).get('purpose', 'train')) if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', tag) else None
    if assessment and ((prepared or {}).get('source_dataset') or dataset).get('manifest_sha256') != assessment.get('identity', {}).get('dataset_manifest_sha256'):
        assessment = {**assessment, 'allowed': False, 'blockers': ['prepared_dataset_manifest_mismatch'],
                      'full_run': {'state': 'blocked', 'blockers': ['prepared_dataset_manifest_mismatch'], 'limitations': []}, 'diagnostics': {'state': 'blocked'}}
    return {"name": name, "run_id": finetune_catalog.run_identity(fp.RUNS, name), "full_training": assessment,
            "evaluation_availability": fe.availability(d), "prepared": prepared, "reexport": fp.reexport_params(prepared or {}), "cpu_validation": util.read_json(d / fp.VALIDATION),
            "state": fp.run_state(d), "imports": fi.list_imports(d), "trial_imports": trial_import.list_imports(d), "eval_imports": fe.list_imports(d), "local_gpu": gpu_local.latest(d),
            "local_train": gpu_local.latest(d, "train")}


@router.get("/runs/{name}/local-fit")
def run_local_fit(name: str, model: str = ""):
    """Whether this computer's graphics card can train the run, for ``model`` (the run's pinned base when empty): the memory the model needs at least and at the
    full context, against what is free now. Reads the model's configuration (a copy on this computer, else the Hub's metadata); nothing is loaded."""
    d, err = _run_dir(name)
    if err:
        return err
    return _bad(410, 'Desktop training is retired. Prepare a portable trial or full bundle, run its notebook in a remote runtime, then import its outputs.')


@router.get("/runs/{name}/curves")
def run_curves(name: str):
    """The loss, gradient norm, learning rate and per-field losses of the run (and its local GPU smoke runs), by optimizer step."""
    d, err = _run_dir(name)
    if err:
        return err
    from app.backend.pipeline import run_curves as rc
    return rc.curves(d)


class CheckImportIn(BaseModel):
    kind: Literal["outputs", "eval", "trial"]
    path: str
    evaluation_dataset_dir: str | None = None


@router.post("/runs/{name}/check-import")
def check_import(name: str, body: CheckImportIn):
    """Check an ``outputs.zip`` (kind outputs) or an ``eval_outputs.zip`` (kind eval) from a GPU run against this prepared run, on a copy that is thrown away, and say how it did
    as a list of steps in plain words. Nothing is imported, kept or scored; the import itself is the job that follows."""
    d, err = _run_dir(name)
    if err:
        return err
    path = pathlib.Path(body.path.strip().strip('"'))
    if not body.path.strip() or not path.is_file():
        return _bad(400, "That file is not there. Check the path of the zip file.")
    from app.backend.pipeline import import_guide
    try:
        return import_guide.check(body.kind, path, d, experiments=pathlib.Path("experiments"),
            **({'evaluation_dataset_dir': body.evaluation_dataset_dir} if body.kind == 'eval' and body.evaluation_dataset_dir else {}))
    except (OSError, ValueError) as e:
        return _bad(400, f"The file could not be checked: {e}")


@router.get("/runs/{name}/bundle")
def run_bundle(name: str):
    d, err = _run_dir(name)
    if err:
        return err
    return FileResponse(d / "bundle.zip", media_type="application/zip", filename=f"{name}-bundle.zip")


@router.get("/runs/{name}/notebook")
def run_notebook(name: str):
    d, err = _run_dir(name)
    if err:
        return err
    from app.backend.pipeline import finetune_prep as fp
    try:
        path = fp.refresh_notebook_workspace(run_dir=d)
    except (ValueError, OSError, RuntimeError) as problem:
        return _bad(409, f'The notebook could not be prepared for download: {problem}')
    return FileResponse(path, media_type="application/x-ipynb+json", filename=f"{name}.ipynb")


@router.get("/runs/{name}/imports/{import_id}/analytics")
def run_analytics(name: str, import_id: str):
    import re
    import json
    from app.backend.pipeline.finetune_import import _sha_file
    d, err = _run_dir(name)
    if err:
        return err
    if not re.fullmatch(r"[0-9a-f]{16}", import_id):
        return _bad(400, "bad import ID")
    folder = d / "imports" / import_id
    path = folder / "training_analytics.json"
    if not (folder / "import.json").is_file() or not path.is_file():
        return _bad(404, "No imported analytics report is available")
    try:
        manifest = json.loads((folder / "outputs_manifest.json").read_text())
        expected = manifest["files"]["training_analytics.json"]["sha256"]
        if _sha_file(path) != expected:
            return _bad(409, "The stored analytics report changed after import")
    except (ValueError, OSError, KeyError, TypeError):
        return _bad(409, "The imported analytics manifest could not be verified")
    return FileResponse(path, media_type="application/json", filename=f"{name}-training-analytics.json")
