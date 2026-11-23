"""Calibration analysis: reliability diagram, ECE, Brier score, temperature scaling.

Answers RQ3 - are the models' confidence scores trustworthy, and does temperature
scaling improve them? Open-weight Arms B and C expose real logits, which is one
practical reason for including them rather than relying on an API model alone.

Temperature scaling (Guo et al., 2017) divides the logits by a single scalar T
fitted on the validation split by minimising negative log-likelihood, then the
calibration on the comparison input (a file the owner uploads to the notebook)
is reported before and after. T is fitted on validation only.
"""
import json
import pathlib

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def expected_calibration_error(conf, correct, n_bins: int = 10):
    conf = np.asarray(conf, dtype=float)
    correct = np.asarray(correct, dtype=float)
    edges = np.linspace(0, 1, n_bins + 1)
    ece = 0.0
    bins = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (conf > lo) & (conf <= hi)
        if not m.any():
            bins.append({"lo": float(lo), "hi": float(hi), "n": 0, "acc": None, "conf": None})
            continue
        acc, avg = correct[m].mean(), conf[m].mean()
        ece += m.mean() * abs(acc - avg)
        bins.append({"lo": float(lo), "hi": float(hi), "n": int(m.sum()),
                     "acc": float(acc), "conf": float(avg)})
    return float(ece), bins


def brier_score(conf, correct) -> float:
    conf = np.asarray(conf, dtype=float)
    correct = np.asarray(correct, dtype=float)
    return float(np.mean((conf - correct) ** 2))


def fit_temperature(val_logits, val_labels, max_iter: int = 200):
    """Fit a single temperature on validation logits by minimising NLL.

    val_logits: (n, n_classes) array of raw logits.
    val_labels: (n,) integer class indices.
    Returns the scalar temperature T >= 0.05.
    """
    import torch
    logits = torch.tensor(np.asarray(val_logits), dtype=torch.float)
    labels = torch.tensor(np.asarray(val_labels), dtype=torch.long)
    T = torch.nn.Parameter(torch.ones(1))
    opt = torch.optim.LBFGS([T], lr=0.01, max_iter=max_iter)
    nll = torch.nn.CrossEntropyLoss()

    def closure():
        opt.zero_grad()
        loss = nll(logits / T.clamp(min=0.05), labels)
        loss.backward()
        return loss

    opt.step(closure)
    return float(T.detach().clamp(min=0.05).item())


def apply_temperature(logits, T):
    logits = np.asarray(logits, dtype=float) / max(T, 0.05)
    e = np.exp(logits - logits.max(axis=1, keepdims=True))
    return e / e.sum(axis=1, keepdims=True)


def reliability_diagram(bins, tag, outpath, bins_after=None):
    fig, ax = plt.subplots(figsize=(4.2, 4.2), dpi=160)
    ax.plot([0, 1], [0, 1], "--", color="#999", linewidth=1, label="perfect")
    xs = [b["conf"] for b in bins if b["n"]]
    ys = [b["acc"] for b in bins if b["n"]]
    ax.plot(xs, ys, "o-", color="#0b4f6c", linewidth=1.6, label=f"{tag}")
    if bins_after:
        xa = [b["conf"] for b in bins_after if b["n"]]
        ya = [b["acc"] for b in bins_after if b["n"]]
        ax.plot(xa, ya, "s-", color="#c1440e", linewidth=1.4, label=f"{tag} (T-scaled)")
    ax.set_xlabel("mean predicted confidence")
    ax.set_ylabel("observed accuracy")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(outpath)
    plt.close(fig)
    print("figure written to", outpath)


def analyse(tag: str, outdir: str = "experiments", figdir: str = "figures"):
    """Calibration from a predictions.csv carrying a `confidence` column.

    If the experiment folder also holds compare_logits.npy / compare_labels.npy and
    val_logits.npy / val_labels.npy (written by the earlier classification notebooks),
    temperature scaling is fitted on validation and the before/after calibration
    on the comparison input is reported.
    """
    d = pathlib.Path(outdir) / tag
    df = pd.read_csv(d / "predictions.csv").dropna(subset=["pred", "confidence"])
    if "true" not in df.columns and "truth" in df.columns:
        df = df.rename(columns={"truth": "true"})
    correct = (df.pred == df["true"]).astype(float)

    ece, bins = expected_calibration_error(df.confidence, correct)
    brier = brier_score(df.confidence, correct)
    out = {"tag": tag, "n": int(len(df)), "ece": ece, "brier": brier, "bins": bins}

    bins_after = None
    tl, ty = d / "compare_logits.npy", d / "compare_labels.npy"
    if not tl.exists():                                   # runs made before 2026-10-02
        tl, ty = d / "test_logits.npy", d / "test_labels.npy"
    vl, vy = d / "val_logits.npy", d / "val_labels.npy"
    if all(p.exists() for p in (tl, ty, vl, vy)):
        T = fit_temperature(np.load(vl), np.load(vy))
        probs = apply_temperature(np.load(tl), T)
        conf_after = probs.max(1)
        pred_after = probs.argmax(1)
        correct_after = (pred_after == np.load(ty)).astype(float)
        ece_a, bins_after = expected_calibration_error(conf_after, correct_after)
        brier_a = brier_score(conf_after, correct_after)
        out.update({"temperature": T, "ece_after": ece_a, "brier_after": brier_a})
        print(f"{tag}: T={T:.3f}  ECE {ece:.3f}->{ece_a:.3f}  Brier {brier:.3f}->{brier_a:.3f}")

    (d / "calibration.json").write_text(json.dumps(out, indent=2))
    pathlib.Path(figdir).mkdir(parents=True, exist_ok=True)
    reliability_diagram(bins, tag, pathlib.Path(figdir) / f"reliability_{tag}.png", bins_after)
    print(f"{tag}: ECE={ece:.3f}  Brier={brier:.3f}  n={len(df)}")
    return out


if __name__ == "__main__":
    import sys
    analyse(sys.argv[1] if len(sys.argv) > 1 else "arm_a_zeroshot")
