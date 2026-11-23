"""Student correction (optional, off by default): train on the student's own verified mistakes, with safeguards.

After a selected SFT checkpoint exists, the student is run on training-pool prompts; where its answer differs from the reviewed target the
case can be exported for an **independent** adjudication and, once adjudicated, imported as a corrected training example. The safeguards:

* only the train pool is ever exported (validation never is), after a selected checkpoint exists, in at most the contract's rounds;
* at most a fixed share (20 percent) of any task's examples can be corrections;
* an imported correction needs an adjudicator who is not the student, marked independent, with a known verdict, and a corrected answer
  that satisfies the response contract;
* spans copied from the student's own output (observations) are masked out of the loss.
"""
from __future__ import annotations

import dataclasses
import math
from collections import Counter

from training.finetune import tokenization as tk

VERDICTS = ("student_wrong", "target_wrong", "both_wrong")


def plan_corrections(errors, task_totals: dict, *, round_index: int, selected_checkpoint: str | None, cfg) -> dict:
    if cfg.enabled_after_selected_sft and not selected_checkpoint:
        raise ValueError("student correction needs a selected checkpoint: select the SFT checkpoint on validation first")
    if not 1 <= round_index <= cfg.maximum_rounds:
        raise ValueError(f"round {round_index} is outside 1..{cfg.maximum_rounds}")
    caps = {t: int(math.floor(cfg.maximum_relevant_task_share * n + 1e-9)) for t, n in task_totals.items()}
    accepted, rejected, used = [], [], Counter()
    for e in sorted(errors, key=lambda e: e["view_id"]):
        if e.get("split") != "train":
            rejected.append({"view_id": e["view_id"], "reason": "not_train_pool"})
        elif used[e["task"]] >= caps.get(e["task"], 0):
            rejected.append({"view_id": e["view_id"], "reason": "task_cap"})
        else:
            used[e["task"]] += 1
            accepted.append(e)
    return {"accepted": accepted, "rejected": rejected, "caps": caps}


def export_rows(accepted, *, student_model_id: str, round_index: int) -> list[dict]:
    """What an independent adjudicator receives; ``adjudication`` and ``corrected`` are filled in by them."""
    return [{"view_id": e["view_id"], "task": e["task"], "family_id": e.get("family_id"), "round": round_index,
             "student_model_id": student_model_id, "student_output": e["student_output"], "target": e["target"],
             "adjudication": None, "corrected": None} for e in accepted]


def import_corrections(rows, *, student_model_id: str, validate) -> dict:
    """Accepted corrections and the reason for every rejected one. ``validate(task, answer)`` returns the problems of an answer."""
    accepted, rejected = [], []
    for r in rows:
        adj = r.get("adjudication")
        reason = None
        if not adj:
            reason = "no_adjudication"
        elif adj.get("adjudicator") == student_model_id:
            reason = "adjudicator_is_the_student"
        elif not adj.get("independent"):
            reason = "not_independent"
        elif adj.get("verdict") not in VERDICTS:
            reason = "unknown_verdict"
        elif validate(r["task"], r.get("corrected")):
            reason = "invalid_corrected_answer"
        (rejected.append({"view_id": r.get("view_id"), "reason": reason}) if reason else accepted.append(r))
    return {"accepted": accepted, "rejected": rejected}


def mask_spans(enc: tk.Encoded, offsets, spans) -> tk.Encoded:
    """The encoding with the labels of every token that overlaps a character span set to ignore (observation text is not supervised)."""
    labels = list(enc.labels)
    for i, (start, end) in enumerate(offsets):
        if end > start and any(start < se and end > ss for ss, se in spans):
            labels[i] = tk.IGNORE
    return dataclasses.replace(enc, labels=labels)
