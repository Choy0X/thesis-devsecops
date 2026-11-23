"""Vulnerable-code detection with any OpenAI-compatible model (base or fine-tuned).

The original model and the fine-tuned model (served by the app's llama.cpp server or by vLLM after the
detection notebook) are run with the same prompt on the same rows of the
detection JSONL, and both runs are scored by ``eval/detection_metrics.py``; the
Compare panel then puts them side by side. The prompt is the one the notebook
trains on (``DETECT_SYSTEM`` / ``build_prompt``): keep the two identical.

When the endpoint returns token log-probabilities, ``score`` is the probability
of "vulnerable" against "safe" for the first answer token, so the decision
threshold chosen on val in the notebook can be applied (``threshold``); without
log-probabilities the model's word is the prediction.
"""
from __future__ import annotations
import json
import math
import pathlib
import random
import time

import pandas as pd

from app.backend.pipeline import llm_client

PROMPT_VERSION = "detect-v1"
DETECT_SYSTEM = ("You are a security code reviewer. Decide whether the code below contains a "
                 "security vulnerability. Answer with exactly one word: vulnerable or safe.")
MAX_CODE_CHARS = 8000


def build_prompt(code: str, language: str | None) -> str:
    return f"Language: {language or 'unknown'}\n\nCode:\n{(code or '')[:MAX_CODE_CHARS]}"


def parse_label(text: str | None) -> int | None:
    t = (text or "").strip().lower().strip("`*\"'. \n")
    if t.startswith("vuln") or t.startswith("yes"):
        return 1
    if t.startswith("safe") or t.startswith("not vuln") or t.startswith("no"):
        return 0
    return None


def _score(choice) -> float | None:
    """P(vulnerable) from the first answer token's top log-probabilities."""
    try:
        top = choice.logprobs.content[0].top_logprobs
    except (AttributeError, IndexError, TypeError):
        return None
    pv = sum(math.exp(t.logprob) for t in top if t.token.strip().lower().startswith("v"))
    ps = sum(math.exp(t.logprob) for t in top if t.token.strip().lower().startswith("s"))
    return pv / (pv + ps) if pv + ps > 0 else None


def detect(code: str, language: str | None, retries: int = 3) -> tuple[int | None, float | None]:
    from app.backend.pipeline.models import arm_a_api
    client = arm_a_api._llm()
    msgs = [{"role": "system", "content": DETECT_SYSTEM},
            {"role": "user", "content": build_prompt(code, language)}]
    logprobs = True
    for _ in range(retries):
        try:
            kw = dict(model=arm_a_api.MODEL, temperature=0, max_tokens=4, messages=msgs)
            if logprobs:
                kw.update(logprobs=True, top_logprobs=5)
            r = client.chat.completions.create(**kw)
            ch = r.choices[0]
            return parse_label(ch.message.content), _score(ch)
        except Exception as e:                       # e.g. no log-probability support
            if logprobs and "logprob" in str(e).lower():
                logprobs = False
                continue
            print("retry:", e)
            time.sleep(3)
    return None, None


def run(n: int = 300, split: str = "val", tag: str | None = None, dataset: str | None = None,
        threshold: float | None = None, seed: int = 42, progress=None, workers=None) -> dict:
    """Detect vulnerable code in a sample with the configured model. ``workers`` requests are in flight at once (None: 2); the rows are kept in
    the order of the sample."""
    from app.backend import compute
    from app.backend.pipeline import runs
    from app.backend.pipeline.eval import detection_metrics
    from app.backend.pipeline.models import arm_a_api
    arm_a_api._llm()                                 # no endpoint -> stop before any work
    path = pathlib.Path(dataset) if dataset else detection_metrics.default_dataset()
    if not path.exists():
        raise FileNotFoundError(f"{path} not found: build the code-pair dataset first")
    rows = [json.loads(l) for l in path.open(encoding="utf-8")]
    if split and split != "all":
        rows = [r for r in rows if r.get("split") == split]
    if n and n < len(rows):
        rows = random.Random(seed).sample(rows, n)
    tag, d = runs.new_run(tag or f"detect_{runs.slug(arm_a_api.MODEL)}")
    runs.write_config(d, {"task": "detection", **llm_client.engine_fields(arm_a_api._CFG), "model": arm_a_api.MODEL,
                          "base_url": llm_client.resolved(arm_a_api._CFG).base_url, "temperature": 0,
                          "dataset": str(path), "split": split, "n": len(rows), "sample_seed": seed,
                          "threshold": threshold, "prompt_version": PROMPT_VERSION,
                          "system_prompt": DETECT_SYSTEM,
                          "prompt_template": build_prompt("<code>", "<language>")})
    def one(r):
        pred, score = detect(r["code"], r.get("language"))
        if threshold is not None and score is not None:
            pred = int(score >= threshold)
        return {"id": r["id"], "pred": pred, "score": score, "label": r["label"], "kind": r.get("kind"), "language": r.get("language"),
                "cve": r.get("cve"), "file": r.get("file"), "pair_id": r.get("pair_id")}

    def report(i, total):
        if progress and (i % 5 == 0 or i == total):
            progress(state="running", label="Detecting", processed=i, total=total)
        if i % 50 == 0 or i == total:
            print(f"{i}/{total} rows")
    out = compute.tmap(one, rows, workers=compute.io_workers(2, workers), progress=report)
    df = pd.DataFrame(out)
    df.to_csv(d / "predictions.csv", index=False)
    metrics = detection_metrics.evaluate(df, tag, dataset=path)
    from app.backend.pipeline.eval import task_attempts
    task_attempts.attach(d, task_attempts.detection_attempts(df, arm=runs.slug(arm_a_api.MODEL), split=split))          # the same answers, scored by the shared evaluator as well
    return metrics
