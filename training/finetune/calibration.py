"""Calibration and operating points, chosen on validation only. Needs torch and scipy.

* ``fit_temperature``: multiclass temperature scaling of the decision head on the calibration role. It changes confidence, never
  the predicted class. With fewer negatives than ``calibration.min_negatives`` nothing is calibrated: the result says so and
  carries only an interval (zero false positives in n negatives bounds the false-positive rate at 95 percent by ``1 - 0.05^(1/n)``).
  That is an interval, not a certification, and scans that cluster by project need an analysis that respects the clusters.
* ``recall_at_fpr``: the highest recall whose false-positive rate on the negatives stays within the target; a tie at the threshold
  is never resolved in favour of more false positives. With too few negatives the point is reported with its interval and marked
  insufficient.
"""
from __future__ import annotations

import math

import torch
import torch.nn.functional as F

from training.finetune import head as hd

NOTE = ("an interval, not a certification: with few negatives the false-positive rate is only bounded, and scans that cluster by "
        "project need a clustering-aware analysis")


def apply_temperature(logits: torch.Tensor, temperature: float) -> torch.Tensor:
    return torch.softmax(logits.float() / temperature, dim=-1)


def vulnerability_scores(logits: torch.Tensor, temperature: float = 1.0) -> torch.Tensor:
    """Log-odds of ``vulnerability_supported`` against the other classes, after temperature scaling."""
    return hd.vulnerability_score(logits.float() / temperature)


def expected_calibration_error(probs: torch.Tensor, labels: torch.Tensor, bins: int = 10) -> float:
    conf, pred = probs.max(-1)
    correct = (pred == labels).float()
    edges = torch.linspace(0, 1, bins + 1)
    total = 0.0
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (conf > lo) & (conf <= hi) if lo > 0 else (conf >= lo) & (conf <= hi)
        if m.any():
            total += float(m.float().mean()) * abs(float(correct[m].mean()) - float(conf[m].mean()))
    return total


def binomial_upper_bound(k: int, n: int, alpha: float = 0.05) -> float:
    """One-sided Clopper-Pearson upper bound on a rate after ``k`` events in ``n`` trials (``1 - alpha^(1/n)`` for k = 0)."""
    if n <= 0 or k >= n:
        return 1.0
    if k == 0:
        return 1 - alpha ** (1 / n)
    from scipy import stats
    return float(stats.beta.ppf(1 - alpha, k + 1, n - k))


def fit_temperature(logits: torch.Tensor, labels: torch.Tensor, *, min_negatives: int) -> dict:
    n = int(labels.numel())
    negatives = int((labels != 0).sum())                       # class 0 is vulnerability_supported
    base = {"method": "temperature_scaling", "n": n, "negatives": negatives, "min_negatives": min_negatives}
    if negatives < min_negatives:
        return {**base, "status": "insufficient_negatives", "temperature": 1.0,
                "fpr_upper_bound_at_zero": binomial_upper_bound(0, negatives) if negatives else 1.0, "note": NOTE}
    logits = logits.float()
    log_t = torch.zeros(1, requires_grad=True)
    opt = torch.optim.LBFGS([log_t], lr=0.1, max_iter=200, line_search_fn="strong_wolfe")

    def closure():
        opt.zero_grad()
        loss = F.cross_entropy(logits / log_t.exp(), labels)
        loss.backward()
        return loss
    opt.step(closure)
    t = float(log_t.detach().exp().clamp(0.05, 20.0))
    before, after = apply_temperature(logits, 1.0), apply_temperature(logits, t)
    return {**base, "status": "calibrated", "temperature": t,
            "nll_before": float(F.cross_entropy(logits, labels)), "nll_after": float(F.cross_entropy(logits / t, labels)),
            "ece_before": expected_calibration_error(before, labels), "ece_after": expected_calibration_error(after, labels)}


def recall_at_fpr(scores: torch.Tensor, labels: torch.Tensor, fpr_target: float, *, min_negatives: int) -> dict:
    """Recall at the strictest threshold whose false-positive rate is within ``fpr_target``. A row is predicted vulnerable
    when its score is at least ``threshold``; labels are 1 for vulnerable and 0 otherwise."""
    pos = scores[labels == 1].float()
    neg = scores[labels != 1].float()
    n_pos, n_neg = int(pos.numel()), int(neg.numel())
    out = {"target_fpr": fpr_target, "n_pos": n_pos, "negatives": n_neg, "insufficient": n_neg < min_negatives}
    if n_neg == 0:
        return {**out, "threshold": None, "fpr": None, "recall": None, "false_positives": 0, "fpr_upper_bound": 1.0}
    allowed = min(int(math.floor(fpr_target * n_neg + 1e-9)), n_neg)
    ordered = torch.sort(neg, descending=True).values
    t = float(ordered[allowed]) if allowed < n_neg else -math.inf
    above = scores.float()[scores.float() > t]
    threshold = float(above.min()) if above.numel() else math.inf              # predicted vulnerable iff score >= threshold
    fp = int((neg >= threshold).sum())
    tp = int((pos >= threshold).sum())
    return {**out, "threshold": threshold, "fpr": fp / n_neg, "false_positives": fp,
            "recall": tp / n_pos if n_pos else None, "fpr_upper_bound": binomial_upper_bound(fp, n_neg)}


def operating_points(scores: torch.Tensor, labels: torch.Tensor, *, min_negatives: int, targets=None) -> list[dict]:
    if targets is None:
        from app.backend import project_config
        targets = project_config.config().evaluation.illustrative_fpr_operating_points
    return [recall_at_fpr(scores, labels, float(t), min_negatives=min_negatives) for t in targets]
