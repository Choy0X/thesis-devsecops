"""Inference: greedy generation from the serialized prompts, and the attempts it gives the shared evaluator.

The prompts are exactly the ones training used (``serialization.serialize_prompt``). Generation is greedy (``do_sample=False``) with the
contract's token limits (``evaluation.greedy_max_new_tokens``, ``repair_max_new_tokens``). Every view that can be shown to the model gets an
attempt; a view whose prompt is over the token budget is excluded with its reason and counted, never silently dropped. A generation that does
not parse as the response contract is a ``malformed`` attempt with the reason (and a note when it was cut off at the token limit); nothing is
repaired. Legacy protocols ignore trailing text after the first JSON object; security_chat_v2 requires one strict complete object. A trained decision head
adds a ``head_decision`` attempt per discovery view from the same prompt (its calibrated log-odds of a supported finding is the score).

Repair attempts carry the oracle report as input and ``verification: unavailable``: no receipt exists for a model patch, so none is a success.
Torch is imported here; the attempt schema itself is ``attempts.py`` (no torch), shared with the app.
"""
from __future__ import annotations

import json
import pathlib
import time
from dataclasses import dataclass

import torch

from training.finetune import attempts as at, head as hd, model as md, serialization as sz, tokenization as tk

STATUS_TO_LABEL, PAIR_POOLS = at.STATUS_TO_LABEL, at.PAIR_POOLS
TRACK_OF_TASK = {"discovery": "generated_decision", "repair": "repair"}
RECORD_COLUMNS = ("record_id", 'issue_group_id', "repository_identity", "ecosystem", "language", "severity_label", "file_path_after",
                  'source_snapshots', 'relevant_parent_commit', 'fix_commit')


@dataclass(frozen=True)
class Generation:
    text: str
    new_tokens: int
    hit_limit: bool                  # the generation reached the limit without ending (no EOS)
    seconds: float
    batch_size: int | None = None


def automatic_batch_size(device):
    if not str(device).startswith('cuda'): return 1
    try: free, _ = torch.cuda.mem_get_info(device)
    except (RuntimeError, ValueError): return 1
    gib = free / 1024 ** 3
    return 32 if gib >= 48 else 16 if gib >= 24 else 8 if gib >= 12 else 4 if gib >= 6 else 1


def load_record_facts(data_dir) -> dict[str, dict]:
    """{record id: the facts the results are broken down by}; columns the dataset does not have are left out."""
    import pyarrow.parquet as pq
    path = pathlib.Path(data_dir) / "records.parquet"
    have = set(pq.ParquetFile(path).schema_arrow.names)
    table = pq.read_table(path, columns=[c for c in RECORD_COLUMNS if c in have])
    return {r["record_id"]: r for r in table.to_pylist()}


# -- generation ------------------------------------------------------------------------------------------------------------------------------------

def _pad_id(tokenizer) -> int:
    return tokenizer.pad_token_id if tokenizer.pad_token_id is not None else tokenizer.eos_token_id


@torch.no_grad()
def generate(model, tokenizer, prompts, *, max_new_tokens: int, device, batch_size: int = 4) -> list[Generation]:
    """Greedy continuation of each prompt, in the order given. Prompts are batched by length and left-padded; the batch size does not
    change a greedy answer."""
    if batch_size <= 0: raise ValueError('generation batch size must be positive')
    was_training, side = model.training, tokenizer.padding_side
    model.eval()
    tokenizer.padding_side = "left"
    eos, pad = tokenizer.eos_token_id, _pad_id(tokenizer)
    out: list[Generation | None] = [None] * len(prompts)
    order = sorted(range(len(prompts)), key=lambda i: len(prompts[i]))
    try:
        for start in range(0, len(order), batch_size):
            chunk = order[start:start + batch_size]
            enc = tokenizer([prompts[i] for i in chunk], return_tensors="pt", padding=True, add_special_tokens=False).to(device)
            began = time.perf_counter()
            retry = False
            try:
                ids = model.generate(**enc, max_new_tokens=max_new_tokens, do_sample=False, pad_token_id=pad, eos_token_id=eos, use_cache=True)
            except torch.OutOfMemoryError:
                if len(chunk) == 1: raise
                del enc
                retry = True
            if retry:
                # Leave the exception handler before retrying so its traceback
                # cannot keep the failed generation's accelerator tensors alive.
                import gc
                gc.collect(); torch.cuda.empty_cache()
                smaller = generate(model, tokenizer, [prompts[i] for i in chunk], max_new_tokens=max_new_tokens,
                                   device=device, batch_size=max(1, len(chunk) // 2))
                for i, result in zip(chunk, smaller): out[i] = result
                continue
            per = (time.perf_counter() - began) / len(chunk)
            new = ids[:, enc["input_ids"].shape[1]:].tolist()
            for i, tokens in zip(chunk, new):
                ended = eos in tokens
                kept = tokens[:tokens.index(eos)] if ended else tokens
                out[i] = Generation(tokenizer.decode(kept, skip_special_tokens=True), len(kept), not ended, per, len(chunk))
    finally:
        tokenizer.padding_side = side
        model.train(was_training)
    return out                                                                       # type: ignore[return-value]


@torch.no_grad()
def head_scores(model, head, tokenizer, prompts, *, device, dtype, batch_size: int = 4):
    """(decision logits (n, 3), seconds): the head over the hidden state at the last prompt position, as in training."""
    was, side = model.training, tokenizer.padding_side
    model.eval()
    head.eval()
    tokenizer.padding_side = "right"
    cuda = str(device).startswith("cuda")
    out, began = [], time.perf_counter()
    try:
        for start in range(0, len(prompts), batch_size):
            chunk = list(prompts[start:start + batch_size])
            enc = tokenizer(chunk, return_tensors="pt", padding=True, add_special_tokens=False).to(device)
            retry = False
            try:
                with torch.autocast(device_type="cuda" if cuda else "cpu", dtype=dtype, enabled=cuda and dtype != torch.float32):
                    hidden = md.hidden_states(model, enc["input_ids"], enc["attention_mask"])
                out.append(head(hd.pooled_prompt_state(hidden, enc["attention_mask"].sum(1))).float().cpu())
                del hidden, enc
            except torch.OutOfMemoryError:
                if len(chunk) == 1: raise
                if 'hidden' in locals(): del hidden
                del enc
                retry = True
            if retry:
                import gc
                gc.collect(); torch.cuda.empty_cache()
                logits, _ = head_scores(model, head, tokenizer, chunk, device=device, dtype=dtype, batch_size=max(1, len(chunk) // 2))
                out.append(logits)
    finally:
        tokenizer.padding_side = side
        model.train(was)
    return (torch.cat(out) if out else torch.zeros(0, len(hd.LABELS))), time.perf_counter() - began


# -- attempts --------------------------------------------------------------------------------------------------------------------------------------

def _facts(view: dict, record: dict, arm: str) -> dict:
    return {"arm": arm, "view_id": view["view_id"], **at.facts_of(view, record)}


def _generated_row(task: str, view: dict, record: dict, arm: str, g: Generation, track: str, *, protocol=sz.SERIALIZATION_ID) -> dict:
    row = {"attempt_id": f"{arm}:{view['view_id']}:{track}", "track": track, **_facts(view, record, arm)}
    if task == "repair":
        row.update(repair_input="oracle_report", verification="unavailable")
    answer, problems = (sz.parse_response(task, g.text, protocol=protocol) if protocol in sz.SECURITY_PROTOCOLS
                        else sz.parse_response(task, g.text))
    if answer is None:
        why = problems[0] if problems else "the answer does not follow the response contract"
        return {**row, "status": "malformed", "prediction": None,
                "detail": why + (" (the generation reached the token limit)" if g.hit_limit else "")}
    return {**row, "status": "answered", "prediction": STATUS_TO_LABEL.get(answer["status"]),
            "detail": "ran on after the answer to the token limit" if g.hit_limit else None}


def evaluate_views(model, tokenizer, views, records: dict, *, arm: str, tasks, max_new_tokens: dict, device, dtype=torch.float32,
                   batch_size: int = 4, head=None, temperature: float = 1.0, generate_fn=generate, log=lambda message: None, context=None, protocol=sz.SERIALIZATION_ID):
    """(attempt rows, info) for the discovery and repair views of ``views`` (rows of ``views.parquet``). ``context`` is the run's own context section (its token budget)."""
    rows: list[dict] = []
    excluded: dict[str, str] = {}
    counts: dict[str, dict] = {}
    raw_generations = []
    started = time.perf_counter()
    chosen = [v for v in views if v["task"] in tasks]
    head_rows = 0
    for task in tasks:
        todo, prompts = [], []
        for v in (v for v in chosen if v["task"] == task):
            prompt = sz.serialize_prompt(task, v["context_text"], protocol=protocol, tokenizer=tokenizer).prompt
            total, reserve = tk.limits(task, context=context)
            if tk.token_count(tokenizer, prompt) > total - reserve:
                excluded[v["view_id"]] = "prompt_over_budget"
                continue
            todo.append(v)
            prompts.append(prompt)
        if not todo:
            continue
        log(f"{task}: generating {len(todo)} answers (up to {max_new_tokens[task]} new tokens each)")
        generations = generate_fn(model, tokenizer, prompts, max_new_tokens=max_new_tokens[task], device=device, batch_size=batch_size)
        if len(generations) != len(todo):
            raise RuntimeError(f'generation count differs from selected cohort: {len(generations)} outputs for {len(todo)} {task} views')
        for v, g, prompt in zip(todo, generations, prompts):
            import hashlib
            raw_generations.append({'view_id': v['view_id'], 'arm': arm, 'task': task, 'text': g.text,
                                      'new_tokens': g.new_tokens, 'hit_limit': g.hit_limit, 'seconds': g.seconds,
                                      'generation_batch_size': getattr(g, 'batch_size', None),
                                    'prompt_sha256': hashlib.sha256(prompt.encode('utf-8')).hexdigest()})
            rows.append(_generated_row(task, v, records.get(v["record_id"], {}), arm, g, TRACK_OF_TASK[task], protocol=protocol))
        if head is not None and task == "discovery":
            logits, _ = head_scores(model, head, tokenizer, prompts, device=device, dtype=dtype, batch_size=batch_size)
            for v, z in zip(todo, logits):
                scaled = z / temperature
                rows.append({"attempt_id": f"{arm}:{v['view_id']}:head_decision", "track": "head_decision", "status": "answered",
                             "prediction": hd.LABELS[int(scaled.argmax())], "score": float(hd.vulnerability_score(scaled)),
                             **_facts(v, records.get(v["record_id"], {}), arm)})
            head_rows += len(todo)
    for r in rows:
        counts.setdefault(r["track"], {})
        counts[r["track"]][r["status"]] = counts[r["track"]].get(r["status"], 0) + 1
    info = {"views": len(chosen), "attempted": len({r["view_id"] for r in rows}), "generated": sum(1 for r in rows if r["track"] != "head_decision"),
            "excluded": excluded, "status_counts": counts, "head": {"temperature": temperature, "scored": head_rows} if head is not None else None,
            "generation": {"max_new_tokens": dict(max_new_tokens), "do_sample": False, "batch_size": batch_size,
                           "seconds": round(time.perf_counter() - started, 1)}}
    info['raw_generations'] = raw_generations
    return rows, info
