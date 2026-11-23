"""The baseline and control runs B0 to B6 of the evaluation plan, as records derived from evidence. No torch.

* B0 the untouched exact base and B1 the untouched Instruct model (an optional initialization reference) exist as evaluated only through
  an evaluation run (a ``run_config.json`` naming the baseline and no adapter, with a ``metrics.json`` beside it).
* B2 base-derived completion-only SFT and B3 the same with field-balanced targets are trained stage-one imports (ordinary loss, and
  ``present_field_normalized_ce``); B4 is a trained stage-two import (the decision head and the pair loss); B5 is a trained import with
  the invariance loss on (one addition at a time). A trained control is *evaluated* only through an evaluation run of that very adapter.
* B6 (verified DPO against chosen-only SFT with matched resources) is blocked: it needs verified preference pairs, which need execution
  receipts, and a preference-training stage, and neither exists yet.

A record has a status (``blocked``, ``trained`` or ``evaluated``) and the reason for it, and never a metric: the numbers live in the
metrics.json of the evaluation run, which the record points to. An incomplete or software-smoke import is never a trained control.
"""
from __future__ import annotations

import json
import pathlib

from app.backend.pipeline import finetune_prep as fp
from app.backend.pipeline import util

STATUSES = ("blocked", "trained", "evaluated")
EXPERIMENTS = pathlib.Path("experiments")
ORDINARY, BALANCED = "per_example_completion_ce", "present_field_normalized_ce"

BASELINES = [
    {"id": "B0", "title": "Untouched exact Qwen2.5-Coder-7B base, fixed plain serialization, no RAG", "optional": False,
     "needs": "an evaluation of the untouched base through the shared evaluator (generation on a GPU runtime)"},
    {"id": "B1", "title": "Untouched Qwen2.5-Coder-7B-Instruct, optional initialization reference with its own serialization", "optional": True,
     "needs": "an evaluation of the untouched Instruct model through the shared evaluator (optional reference)"},
    {"id": "B2", "title": "Base-derived ordinary completion-only SFT on accepted data", "optional": False,
     "needs": "an imported finished stage-1 run with the ordinary completion loss"},
    {"id": "B3", "title": "B2 with field-balanced targets", "optional": False,
     "needs": "an imported finished stage-1 run with the present-field normalized loss"},
    {"id": "B4", "title": "Selected SFT with the reviewed decision and pair objectives", "optional": False,
     "needs": "an imported finished stage-2 run (decision head and pair loss)"},
    {"id": "B5", "title": "Optional invariance or corrected-student-state experiment, one addition at a time", "optional": True,
     "needs": "an imported finished run with the invariance loss on (it is implemented and off by default)"},
    {"id": "B6", "title": "Optional verified DPO versus chosen-only SFT with matched resources", "optional": True,
     "needs": "verified preference pairs (they need execution receipts) and a preference-training stage; neither exists yet"},
]


def _evaluation_runs(experiments: pathlib.Path) -> list[dict]:
    out = []
    root = pathlib.Path(experiments)
    for d in sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []:
        cfg = util.read_json(d / "run_config.json")
        if (isinstance(cfg, dict) and cfg.get("baseline") and (d / "metrics.json").exists()
                and cfg.get('mode') != 'trial' and not cfg.get('experimental') and cfg.get('security_evaluation') is not False
                and not cfg.get("smoke_test") and not cfg.get("partial")
                and cfg.get('counts_as_evaluation') is not False and not cfg.get('excluded')):
            out.append({"tag": d.name, "baseline": cfg["baseline"], "adapter_sha256": cfg.get("adapter_sha256")})
    return out


def _imports(runs: pathlib.Path, experiments: pathlib.Path) -> list[dict]:
    out = []
    root = pathlib.Path(runs)
    for d in sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []:
        if not (d / fp.PREPARED).exists():
            continue
        for i in fp.run_state(d, experiments=experiments)["evidence"].get("imports", []):
            if i["type"] == "trained":
                out.append({**i, "run": d.name})
    return out


def _is(control: str, i: dict) -> bool:
    if control == "B5":
        return bool(i.get("invariance"))
    if i.get("invariance"):
        return False
    if control == "B2":
        return i.get("stage") == 1 and i.get("loss_mode") == ORDINARY
    if control == "B3":
        return i.get("stage") == 1 and i.get("loss_mode") == BALANCED
    if control == "B4":
        return i.get("stage") == 2
    return False


def records(runs=fp.RUNS, experiments=EXPERIMENTS) -> list[dict]:
    imports = _imports(runs, experiments)
    evals = _evaluation_runs(experiments)
    out = []
    for b in BASELINES:
        base = {"id": b["id"], "title": b["title"], "optional": b["optional"]}
        if b["id"] in ("B0", "B1"):
            found = [e["tag"] for e in evals if e["baseline"] == b["id"] and not e["adapter_sha256"]]
            out.append({**base, "status": "evaluated" if found else "blocked",
                        "reason": None if found else f"blocked: {b['needs']}", "evidence": {"imports": [], "evaluations": found}})
            continue
        mine = [i for i in imports if _is(b["id"], i)]
        shas = {i["adapter_sha256"] for i in mine}
        found = [e["tag"] for e in evals if e["baseline"] == b["id"] and e["adapter_sha256"] in shas]
        evidence = {"imports": [{"run": i["run"], "import_id": i["import_id"], "adapter_sha256": i["adapter_sha256"]} for i in mine],
                    "evaluations": found}
        if found:
            out.append({**base, "status": "evaluated", "reason": None, "evidence": evidence})
        elif mine:
            out.append({**base, "status": "trained", "reason": "trained and imported; no evaluation run of this adapter through the shared "
                                                               "evaluator yet", "evidence": evidence})
        else:
            out.append({**base, "status": "blocked", "reason": f"blocked: {b['needs']}", "evidence": evidence})
    return out
