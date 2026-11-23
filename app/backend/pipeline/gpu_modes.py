"""Who has the graphics card: Idle, Train or Serve.

The card is small and shared with the desktop. The mode is read from what is running, never stored: Train when a training or a GPU check holds the lock (``gpu_local``), Serve when the
model gateway has a model on the card, else Idle. The models of the gateway never keep a job from starting: a job takes the card through the gateway's exclusive lease (the models are
unloaded once they have finished their answers, and load again when they are asked for), so only another job counts as a conflict. A job that holds the card is never stopped from here
(it has its own checkpoint-safe cancel); the models can be, because unloading them loses nothing but the time to load them again.
"""
from __future__ import annotations

import pathlib

TRAIN_STAGES = ("train_local", "gpu_smoke", "gpu_eval")
MODES = ("idle", "train", "serve")
ROLE_WORDS = {"llm": "the language model", "embed": "the embedding model"}


def _jobs_running(stages) -> list[str]:
    from app.backend import jobs
    try:
        return [j.stage for j in jobs.running() if j.stage in stages]
    except Exception:  # ignored: a registry that cannot be read means no job is known to hold the card
        return []


def holders() -> list[dict]:
    """What holds the card now, each as ``{mode, what, stoppable}``; a model of the gateway is stoppable here, a job is not."""
    from app.backend.pipeline import gpu_local
    from app.backend.pipeline.gateway import client as gateway
    out = []
    train = _jobs_running(TRAIN_STAGES)
    if train or gpu_local.lock_alive():
        out.append({"mode": "train", "what": "a training or GPU check" + (f" ({train[0].replace('_', ' ')})" if train else ""), "stoppable": False})
    snapshot = gateway.status(timeout=1.0) or {}
    slots = snapshot.get("slots") or {}
    shown = set()
    for role, slot in slots.items():
        if slot.get("state") in ("loading", "ready", "draining") and (slot.get("gpu_bytes") or 0) > 0:
            name = pathlib.PurePath(slot["model"]).name if slot.get("model") else "a model"
            out.append({"mode": "serve", "what": f"{ROLE_WORDS.get(role, role)} ({name})", "stoppable": True})
            if slot.get("ollama_name"):
                shown.add(slot["ollama_name"].removesuffix(":latest"))
    for runner in snapshot.get("running_models", ()):
        name = runner["name"]
        if name.removesuffix(":latest") not in shown and runner.get("size_vram"):
            out.append({"mode": "serve", "what": name, "stoppable": True})
    return out


def current() -> dict:
    """``{mode, holders}``; the mode is the first holder in the order Train, Serve, else Idle."""
    h = holders()
    return {"mode": h[0]["mode"] if h else "idle", "holders": h}


def conflict(wanted: str) -> dict | None:
    """Why ``wanted`` (``train``, ``serve`` or ``embed``) cannot have the card now, or None when it can. Only a training or GPU check is a conflict: the gateway's models give way."""
    if wanted not in ("train", "serve", "embed"):
        raise ValueError(f"unknown GPU mode {wanted!r}")
    others = [h for h in holders() if h["mode"] == "train"]
    if not others:
        return None
    first = others[0]
    return {"wanted": wanted, "held_by": first["mode"], "message": f"The graphics card is in use by {first['what']}. Wait for it to finish, or cancel it from the Jobs list.", "stoppable": False}


def release() -> dict:
    """Free the card: unload the gateway's models once they have finished what they are answering. Jobs are left alone and named in ``still_holding``."""
    from app.backend.pipeline.gateway import client as gateway
    from app.backend.pipeline.gateway.errors import GatewayError
    stopped = []
    try:
        if gateway.status(timeout=1.0):
            stopped = [ROLE_WORDS.get(r, r) for r in gateway.unload()["unloaded"]]
    except GatewayError:
        pass                                                      # the gateway could not unload now; the holders below still say what has the card
    return {"stopped": stopped, "still_holding": [h["what"] for h in holders()], **current()}
