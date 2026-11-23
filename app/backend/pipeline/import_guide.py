"""A check of a file from a GPU run, before it is imported, told as a list of steps in plain words.

``finetune_import`` (an ``outputs.zip``) and ``finetune_eval_import`` (an ``eval_outputs.zip``) already run a fixed series of checks and reject a file whole when one fails. This runs
them on a copy that is thrown away (``dry_run``) and turns what they say into steps: what each step looks at, whether the file passed it, failed it (with the reasons exactly as the
check wrote them) or was not looked at because an earlier step had already failed, and what the person can do about a failure. Nothing is kept and nothing is scored. The steps are
those of the checks themselves, in the order they run; the words about them are written here.
"""
from __future__ import annotations

import pathlib
import zipfile

from app.backend.pipeline import finetune_eval_import as fe, finetune_import as fi

# (name, title, what it looks at, what to do when it fails). A stage is a group of steps that run together; a later stage runs only when the stages before it had no failure.
OUTPUTS = {
    "stages": [("zip_structure",), ("manifest",), ("run_config",), ("identity", "head", "adapter", "consistency", "type")],
    "steps": {
        "zip_structure": ("The file is a sound zip", "It opens as a zip, is not larger than the limit and holds no file that would be written outside the import folder.",
                          "Download outputs.zip again from the GPU runtime; do not unpack and repack it."),
        "manifest": ("Nothing changed after the run wrote it", "outputs_manifest.json lists every file with its hash, every listed file is there and unchanged, and nothing else is in the zip.",
                     "Download the file again. A file that was edited, or a zip that was made by hand, is rejected on purpose."),
        "run_config": ("It is a fine-tuning run record", "run_config.json is the record a fine-tuning run writes.",
                       "This is not the outputs.zip of a fine-tuning run. If it is an evaluation, use the evaluation import."),
        "identity": ("It belongs to this prepared run", "The model and tokenizer commits, the dataset version and its manifest, the bundle, the serializer and the settings are the ones this run was prepared with.",
                     "The file comes from another run, or from this one prepared under other settings. Choose the file of this run, or prepare the run again and train it."),
        "head": ("The decision head is the expected one", "The head's label order, pooling and size are the ones this version of the code uses.",
                 "The run was made by another version of the training code. Train it again with this version."),
        "adapter": ("The adapter is the one the settings describe", "Its rank, alpha, target modules, bias, rslora and DoRA settings are the run's, and it holds the number of parameters the model's architecture needs.",
                    "The adapter was made with other settings or for another model. Train the run again with the settings shown on this page."),
        "consistency": ("The record agrees with its files", "The number of steps in the metrics file is the number the run says it did, the loss mode is a known one, and the evaluations and calibration files can be read.",
                        "A file is damaged or was written by an interrupted run. Download it again, or train the run again."),
        "type": ("What kind of run it is", "A software check, a GPU check, a complete training or an unfinished one; a complete training on the full model must report a graphics card.",
                 "A run that claims to be a full training but reports no graphics card is rejected. Check where it was run."),
    },
}
EVALUATION = {
    "stages": [("zip_structure",), ("manifest",), ("evaluation_config", "identity", "generation", "baseline"), ("adapter",), ("evaluation_dataset",), ("attempts", "coverage", "truth", "facts")],
    "steps": {
        "zip_structure": OUTPUTS["steps"]["zip_structure"],
        "manifest": ("Nothing changed after the run wrote it", "eval_outputs_manifest.json lists every file with its hash, every listed file is there and unchanged, and nothing else is in the zip.",
                     "Download the file again. A file that was edited, or a zip that was made by hand, is rejected on purpose."),
        "evaluation_config": ("It is a finished evaluation", "evaluation_config.json is a finished evaluation of the validation split, with a role (selection or calibration) and tasks (discovery, repair) the scorer knows.",
                              "This is not the eval_outputs.zip of a finished evaluation. If it is a training's outputs, use the outputs import."),
        "identity": ("It was made for this run", "The model, tokenizer, original training dataset, training bundle, serializer and settings match this prepared run. Independent evaluation data has its own identity.",
                     "The evaluation comes from another run or from another version. Choose the file of this run."),
        "evaluation_dataset": ("The benchmark is authenticated independently", "The reviewed benchmark files, source revisions, labels and partition roles match their manifest; overlap is recomputed against the original training source.",
                               "Supply the same independently reviewed frozen benchmark directory used for evaluation, or package it during export. Preserve the original training dataset."),
        "generation": ("The answers were made under the expected settings", "Decoding is greedy, and unless it is a software check the answer length limits are the contract's.",
                       "Evaluate again with the settings the notebook gives."),
        "baseline": ("The control it stands for can be imported", "It is B0, B2, B3, B4 or B5, and B0 is the untouched pinned base with no adapter.",
                     "Choose one of those controls in the notebook and evaluate again."),
        "adapter": ("The adapter it used was imported into this run", "A trained control names an adapter that was imported into this run as a trained run, from the same bundle, and trained as that control.",
                    "Import the outputs of the training first, then the evaluation."),
        "attempts": ("The attempts are well formed", "The attempts file can be read; each attempt is well formed; all name the arm the evaluation says; none is repeated; they are on allowed tracks and on the validation split; a repair has no execution receipt.",
                     "Evaluate again with the notebook; a file edited by hand is rejected."),
        "coverage": ("Every expected view was answered or excluded", "The views the dataset expects for this role and these tasks are the views in the file: none missing, none outside it, none both excluded and attempted, and a partial run says it is partial.",
                     "Evaluate again so that every expected view is answered or excluded with a reason."),
        "truth": ("The truth is the dataset's", "The truth label of every attempt is the label the dataset has for that view.",
                  "The attempts were made against other data. Evaluate again on this run's dataset."),
        "facts": ("The facts of each attempt are the dataset's", "The issue group, repository, ecosystem, language, pair, role, path, severity and split recorded with each attempt are the dataset's.",
                  "The attempts were made against other data. Evaluate again on this run's dataset."),
    },
}

TRIAL = {
    'stages': [('zip_structure',), ('manifest',), ('trial_config',), ('identity',), ('reports',)],
    'steps': {
        'zip_structure': OUTPUTS['steps']['zip_structure'],
        'manifest': ('Every trial file is unchanged', 'trial_outputs_manifest.json authenticates every member with SHA256 and byte counts.',
                     'Download trial_outputs.zip again from the original trial notebook.'),
        'trial_config': ('The artifact is an experimental trial', 'The state and budget are recorded, with full training and security evaluation explicitly disabled.',
                         'Use the portable trial notebook and its trial_outputs.zip download.'),
        'identity': ('The original pinned trial inputs match', 'The original bundle, resolved configuration, derived data manifest and model/tokenizer commits all match.',
                     'Select the prepared trial that produced this archive. Preserve its bundle and resolved configuration.'),
        'reports': ('Available trial reports can be read', 'Paired loss, raw examples, resource measurements and progress remain experimental; missing reports are allowed for unfinished trials.',
                    'Download the archive again. If the trial stopped early, preserve its partial outputs and recorded reason.'),
    },
}


def steps(kind: dict, failures: list[dict]) -> list[dict]:
    """The steps of one kind of check, each with its state: ``passed``, ``failed`` or ``not_checked``, and the reasons for a failure."""
    by_check: dict[str, list[str]] = {}
    for f in failures:
        by_check.setdefault(str(f.get("check")), []).append(str(f.get("detail")))
    failed_stage = next((i for i, stage in enumerate(kind["stages"]) if any(name in by_check for name in stage)), None)
    unknown = [name for name in by_check if not any(name in stage for stage in kind["stages"])]
    out = []
    for i, stage in enumerate(kind["stages"]):
        for name in stage:
            title, what, fix = kind["steps"][name]
            state = "not_checked" if failed_stage is not None and i > failed_stage else "failed" if name in by_check else "passed"
            out.append({"name": name, "title": title, "what": what, "state": state, "failures": by_check.get(name, []), "fix": fix if state == "failed" else None})
    if unknown:                                    # a check this list does not know: shown with its own words, never dropped
        out.append({"name": "other", "title": "Another check", "what": "A check this page has no description for.", "state": "failed",
                    "failures": [d for name in unknown for d in by_check[name]], "fix": None})
    return out


def check(kind: str, path, run_dir, experiments=None, evaluation_dataset_dir=None) -> dict:
    """Run every check on a copy and say how the file did. ``kind`` is ``outputs`` or ``eval``; ``experiments`` is where the evaluation runs are kept (read, never written)."""
    if kind not in ("outputs", "eval", 'trial'):
        raise ValueError("kind must be outputs, eval or trial")
    path, run_dir = pathlib.Path(path), pathlib.Path(run_dir)
    spec, rejected, run = (OUTPUTS, fi.ImportRejected, fi.import_outputs) if kind == "outputs" else (EVALUATION, fe.EvalRejected, fe.import_eval)
    if kind == 'trial':
        from . import trial_import
        spec, rejected, run = TRIAL, trial_import.ImportRejected, trial_import.import_outputs
    try:
        record = run(path, run_dir, dry_run=True, **({"experiments": experiments} if experiments is not None and kind == "eval" else {}),
                     **({'evaluation_dataset_dir': evaluation_dataset_dir} if evaluation_dataset_dir is not None and kind == 'eval' else {}))
    except zipfile.BadZipFile:
        return {"kind": kind, "ok": False, "already_imported": False, "summary": None,
                "steps": steps(spec, [{"check": "zip_structure", "detail": "the file is not a zip file, or it is damaged"}])}
    except rejected as e:
        return {"kind": kind, "ok": False, "already_imported": False, "steps": steps(spec, e.failures), "summary": None}
    if kind == 'trial':
        return {'kind': kind, 'ok': True, 'already_imported': bool(record.get('already_imported')),
                'steps': steps(spec, []), 'summary': {key: record[key] for key in (
                    'state', 'experimental', 'counts_as_full_training', 'security_evaluation', 'software_test', 'total_budget_minutes', 'reports')}}
    summary = ({"type": record["type"], "state": record["run"]["state"], "steps_done": record["run"]["steps_done"], "adapter_parameters": record["adapter"]["parameters"],
                "smoke_test": bool(record["run"]["smoke_test"]), "warnings": record["warnings"]} if kind == "outputs"
               else {"baseline": record["baseline"], "arm": record["arm"], "role": record["role"], "attempts": record["attempts"], "excluded": record["excluded"],
                     "counts_as_evaluation": record["counts_as_evaluation"], "smoke_test": record["smoke_test"], "partial": record["partial"]})
    return {"kind": kind, "ok": True, "already_imported": bool(record.get("already_imported")), "steps": steps(spec, []), "summary": summary}
