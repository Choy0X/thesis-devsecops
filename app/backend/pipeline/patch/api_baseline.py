"""Patch-generation baseline with the configured OpenAI-compatible model.

Sends test-split code pairs (vulnerable code + CWE + advisory) to the API model
with the shared prompt, stores the predictions and ``run_config.json``,
and scores them with ``eval.patch_metrics`` - the same evaluator the fine-tuned
model's Colab predictions go through.
"""
from __future__ import annotations
import datetime
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from app.backend.pipeline.eval import patch_metrics as pm

TEMPERATURE = 0.0
MAX_CHARS = 12000          # longer inputs exceed small models' context; skipped


def run(n: int = 100, split: str = "val", languages: list[str] | None = None,
        granularity: str | None = "function", tag: str | None = None,
        dataset: str | None = None, workers: int = 2) -> dict:
    from app.backend.pipeline import llm_client
    client, model = llm_client.configured(timeout=180)
    base = llm_client.endpoint_url()
    samples = pm.load_samples(dataset, split, languages, granularity, None)
    long = [x for x in samples if len(x.get("vulnerable_code") or "") > MAX_CHARS]
    samples = [x for x in samples if len(x.get("vulnerable_code") or "") <= MAX_CHARS][:n]
    if long:
        print(f"skipping {len(long)} samples with more than {MAX_CHARS} characters of code")
    if not samples:
        raise RuntimeError("No samples match; build the code-pair dataset or relax the filters.")
    from app.backend.pipeline import runs
    tag, _ = runs.new_run(tag or f"patch_{runs.slug(model)}")
    pm.write_run_config(tag, {
        "arm": "A (API baseline)", "model": model, "base_url": base,
        "temperature": TEMPERATURE, "split": split, "n": len(samples),
        "languages": languages or "all", "granularity": granularity or "all",
        "dataset": str(dataset or pm.default_dataset())})
    print(f"{tag}: generating fixes for {len(samples)} {split} pairs with {model}")

    def gen(s):
        r = client.chat.completions.create(
            model=model, temperature=TEMPERATURE, max_tokens=2048,
            messages=[{"role": "system", "content": pm.PATCH_SYSTEM},
                      {"role": "user", "content": pm.build_prompt(s)}])
        return pm.extract_code(r.choices[0].message.content or "")

    preds = []
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
        futs = {ex.submit(gen, s): s for s in samples}
        for i, fut in enumerate(as_completed(futs), 1):
            s = futs[fut]
            try:
                preds.append({"id": s["id"], "prediction": fut.result()})
                print(f"[{i}/{len(samples)}] {s.get('cve') or '-'} {s.get('function') or s.get('file')}: ok")
            except Exception as e:
                print(f"[{i}/{len(samples)}] {s.get('cve') or '-'}: failed - {str(e)[:120]}")
    if not preds:
        raise RuntimeError("Every generation failed; check the LLM endpoint/model in Settings.")
    df = pd.DataFrame(preds)
    out = pm.pathlib.Path("experiments") / tag
    out.mkdir(parents=True, exist_ok=True)
    df.to_csv(out / "predictions.csv", index=False)
    return pm.evaluate_patches(df, tag, dataset)
