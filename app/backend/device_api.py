"""``GET /api/device``: the processor, memory, disks and graphics card of this computer (see ``device.py``)."""
from __future__ import annotations

from fastapi import APIRouter

from app.backend import device

router = APIRouter()


@router.get("/api/device")
def device_info():
    """Cores, memory, every disk with its free space, and the GPU read from nvidia-smi (explicit and with a reason when there is none)."""
    return device.snapshot()
