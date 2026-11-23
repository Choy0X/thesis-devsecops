"""The decision policy: reconcile what the decision head says with what the model generated. No torch.

The head answers with one of the three label states (``vulnerability_supported`` when its score reaches the operating threshold, else
the likelier of the other two); the generation answers with a status. Agreement gives the shared verdict. A finding against a
no-finding is a material disagreement: the verdict is ``uncertain_review_required`` and it counts as an abstention (never as a
detection). When one side asks for more context that side is followed. A generation that does not parse is an abstention too, with
the head's verdict kept for the record.
"""
from __future__ import annotations

SUP, NOF, INS = "vulnerability_supported", "no_supported_finding_in_scope", "insufficient_context"
UNCERTAIN = "uncertain_review_required"
STATUS_TO_CLASS = {"finding": SUP, "no_finding": NOF, "insufficient_context": INS}


def head_verdict(probs, z: float, threshold: float) -> dict:
    p = [float(x) for x in probs]
    verdict = SUP if z >= threshold else (NOF if p[1] >= p[2] else INS)
    return {"verdict": verdict, "z": float(z), "threshold": float(threshold),
            "probs": {SUP: p[0], NOF: p[1], INS: p[2]}}


def decide(probs, z: float, threshold: float, generated_status: str | None) -> dict:
    head = head_verdict(probs, z, threshold)
    g = STATUS_TO_CLASS.get(generated_status) if generated_status else None
    base = {"head": head, "generated": generated_status}
    if g is None:
        return {**base, "verdict": UNCERTAIN, "abstained": True, "reason": "generation_malformed"}
    if g == head["verdict"]:
        return {**base, "verdict": g, "abstained": False, "reason": "head_and_generation_agree"}
    if {g, head["verdict"]} == {SUP, NOF}:
        return {**base, "verdict": UNCERTAIN, "abstained": True, "reason": "material_disagreement"}
    return {**base, "verdict": INS, "abstained": False, "reason": "one_side_needs_context"}
