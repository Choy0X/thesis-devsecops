"""Routes of the Train page that describe how a run is configured and check a choice of settings. Nothing is started or written here."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.backend import errors

router = APIRouter()


@router.get("/api/train/config")
def train_config():
    """Every setting of the training configuration with its value, limits and whether it is fixed in this phase, grouped by section."""
    from app.backend.pipeline import train_schema
    from training.finetune import config as cfgmod
    try:
        cfg = cfgmod.load()
    except (cfgmod.ConfigError, OSError) as e:
        return errors.error("The training configuration could not be read.", 500, detail=str(e))
    return train_schema.describe(cfg, cfgmod.file_sha256())


@router.get("/api/train/profiles")
def train_profiles():
    """The run profiles: a label, what it is for, why its values were chosen, and the overrides of the contract it stands for."""
    from training.finetune import profiles
    from app.backend.pipeline.finetune_prep import trial_profile
    try:
        return {"profiles": {**profiles.load(), 'portable_trial': trial_profile()}}
    except (ValueError, OSError) as e:
        return errors.error("The run profiles could not be read.", 500, detail=str(e))


class ResolveIn(BaseModel):
    overrides: dict[str, object] = Field(default_factory=dict)


@router.post("/api/train/resolve")
def train_resolve(body: ResolveIn):
    """Check a choice of settings against the contract without writing anything: the problems in plain words, or the settings that differ from the contract
    and a fingerprint of the effective configuration. The fingerprint still has the model commit and the dataset as placeholders; the hash that is kept with a
    run is made when the run is prepared, and it covers the overrides."""
    import hashlib
    import json

    import yaml

    from training.finetune import config as cfgmod, overrides as ov
    base = yaml.safe_load(cfgmod.DEFAULT_PATH.read_text(encoding="utf-8"))
    try:
        effective = ov.apply(base, body.overrides)
    except ov.OverrideError as e:
        return {"ok": False, "problems": e.problems, "changed": [], "fingerprint": None}
    before = cfgmod.parse(base, "contract").model_dump(mode="json")
    after = cfgmod.parse(effective, "contract").model_dump(mode="json")
    blob = json.dumps(after, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"ok": True, "problems": [], "changed": ov.changed(before, after), "fingerprint": hashlib.sha256(blob).hexdigest()}


@router.get("/api/train/schedule")
def train_schedule(families: int, effective_batch: int = 32, epochs: float | None = None, steps: int | None = None, evaluate_every_steps: int | None = None,
                   evaluate_at: str | None = None):
    """What a length of training comes to, by the arithmetic the runner uses: give ``epochs`` (family passes) or ``steps`` and the number of training families, and get the other
    one back with the evaluation points in steps and the steps that follow the last evaluation. Nothing is started."""
    from training.finetune import schedule
    if (epochs is None) == (steps is None):
        return errors.error("Give the length either as epochs or as steps.", 400, hint="Epochs are family passes: the sequences drawn divided by the number of training families.")
    if families < 1 or effective_batch < 1 or (steps is not None and steps < 1) or (epochs is not None and epochs <= 0):
        return errors.error("The families, the batch, the epochs and the steps must be positive.", 400)
    try:
        points = [float(x) for x in evaluate_at.split(",") if x.strip()] if evaluate_at else []
    except ValueError:
        return errors.error("The evaluation points are family passes separated by commas, for example 0.5,1,2,3.", 400)
    return schedule.describe(family_passes=epochs, steps=steps, families=families, effective_batch=effective_batch, evaluate_at_family_passes=points,
                             evaluate_every_steps=evaluate_every_steps)
