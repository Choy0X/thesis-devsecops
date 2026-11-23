"""RQ4 - contamination-controlled anti-pattern flagging and a memorisation probe.

Anti-pattern flagging. Using CVEFixes code pairs, each record contributes a
vulnerable function (``code_before``, a positive) and its fixed counterpart
(``code_after``, a negative). A query is flagged if its maximum embedding
similarity to the historical anti-pattern bank (train-split vulnerable functions
only) exceeds a threshold tuned on validation. Reported on the POST-CUTOFF test
split, so a flag cannot be explained by having seen the record in pre-training.
Outputs the true-positive rate (vulnerable functions flagged) and the
false-alarm rate (fixed functions flagged).

Memorisation probe. The model is shown only a CVE identifier prefix and asked to
complete the description. High overlap with the real description is evidence of
memorisation. The outcome is reported either way - a positive probe is a
finding, not a failure (it bounds what the flagging result can claim).

Both procedures require resources not present in every environment (CVEFixes code
pairs for flagging; a reachable LLM endpoint for the probe), so each checks its
inputs and exits cleanly with an explanation if they are missing.
"""
import json
import pathlib
import argparse

import numpy as np
import pandas as pd

PROC = pathlib.Path("data/processed")
EMBED_MODEL = "all-MiniLM-L6-v2"


def _embed(texts, model):
    return np.asarray(model.encode(list(texts), batch_size=64,
                                   show_progress_bar=False, normalize_embeddings=True))


def anti_pattern_flagging(dataset="data/processed/dataset_v1.parquet",
                          tag="flagging", threshold=None):
    from sentence_transformers import SentenceTransformer
    df = pd.read_parquet(dataset)
    if "code_before" not in df.columns or df.code_before.notna().sum() == 0:
        print("No code pairs in dataset; flagging needs CVEFixes code. Skipping.")
        return None

    model = SentenceTransformer(EMBED_MODEL)
    bank = df[(df.split == "train") & df.code_before.notna()].code_before.str[:4000]
    if len(bank) == 0:
        print("Empty anti-pattern bank. Skipping.")
        return None
    bank_emb = _embed(bank, model)

    def max_sim(texts):
        if len(texts) == 0:
            return np.array([])
        q = _embed(texts, model)
        return (q @ bank_emb.T).max(axis=1)

    # Tune threshold on validation if not supplied: pick the value maximising
    # (TPR - FAR) on val code pairs.
    val = df[(df.split == "val") & df.code_before.notna()]
    if threshold is None and len(val):
        pos = max_sim(val.code_before.str[:4000])
        neg = max_sim(val.code_after.dropna().str[:4000])
        grid = np.linspace(0.2, 0.9, 71)
        best, threshold = -1, 0.5
        for t in grid:
            tpr = (pos >= t).mean() if len(pos) else 0
            far = (neg >= t).mean() if len(neg) else 0
            if tpr - far > best:
                best, threshold = tpr - far, float(t)

    test = df[(df.split == "test") & df.code_before.notna()]
    pos = max_sim(test.code_before.str[:4000])
    neg = max_sim(test.code_after.dropna().str[:4000])
    tpr = float((pos >= threshold).mean()) if len(pos) else None
    far = float((neg >= threshold).mean()) if len(neg) else None

    out = {"tag": tag, "threshold": threshold,
           "n_vuln_test": int(len(pos)), "n_fixed_test": int(len(neg)),
           "true_positive_rate": tpr, "false_alarm_rate": far,
           "embed_model": EMBED_MODEL}
    d = pathlib.Path("experiments") / tag
    d.mkdir(parents=True, exist_ok=True)
    (d / "flagging.json").write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))
    return out


def memorisation_probe(dataset="data/processed/dataset_v1.parquet",
                       tag="memorisation", n=100):
    """Ask the model to complete a CVE description from its identifier only."""
    import os
    from difflib import SequenceMatcher
    from dotenv import dotenv_values
    from app.backend.pipeline import llm_client

    client, model = llm_client.configured(timeout=llm_client.SDK_DEFAULT_TIMEOUT, fallback=dotenv_values(".env"), required=False)
    if client is None:
        print("No LLM endpoint configured; skipping memorisation probe.")
        return None

    df = pd.read_parquet(dataset)
    df = df[df.cve_id.notna() & df.text.notna()].sample(
        min(n, len(df)), random_state=42)

    rows = []
    for row in df.itertuples():
        try:
            r = client.chat.completions.create(
                model=model, temperature=0,
                messages=[{"role": "user",
                           "content": f"Complete the official description for "
                                      f"{row.cve_id}. If unknown, say UNKNOWN."}])
            gen = r.choices[0].message.content or ""
        except Exception as e:
            gen = f"ERROR:{e}"
        overlap = SequenceMatcher(None, gen.lower(), str(row.text).lower()).ratio()
        rows.append({"cve_id": row.cve_id, "overlap": overlap,
                     "said_unknown": "unknown" in gen.lower()[:30]})

    res = pd.DataFrame(rows)
    d = pathlib.Path("experiments") / tag
    d.mkdir(parents=True, exist_ok=True)
    res.to_csv(d / "memorisation.csv", index=False)
    summary = {"tag": tag, "model": model, "n": len(res),
               "mean_overlap": float(res.overlap.mean()),
               "share_high_overlap_ge_0.6": float((res.overlap >= 0.6).mean()),
               "share_said_unknown": float(res.said_unknown.mean())}
    (d / "memorisation.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="data/processed/dataset_v1.parquet")
    ap.add_argument("--probe", action="store_true", help="run the memorisation probe")
    ap.add_argument("-n", type=int, default=100)
    a = ap.parse_args()
    if a.probe:
        memorisation_probe(a.dataset, n=a.n)
    else:
        anti_pattern_flagging(a.dataset)
