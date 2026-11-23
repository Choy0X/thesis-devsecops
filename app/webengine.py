"""The window's graphics: draw it with the graphics card (the default) or without it.

With the setting "Use the graphics card to draw the interface" off (``ZTSA_UI_GPU=off``) the web view is started with Chromium's ``--disable-gpu`` switches and Qt Quick in
software, which helps on a computer where the driver draws the window badly (flicker, a blank page) and costs a little smoothness. The flags are read when the web engine
starts, so this must run before the application object is created, and a change in Settings takes effect at the next start.
"""
from __future__ import annotations

import os

ENV = "ZTSA_UI_GPU"
FLAGS_OFF = ("--disable-gpu", "--disable-gpu-compositing")


def wants_software(env=None) -> bool:
    return (os.environ if env is None else env).get(ENV, "").strip().lower() == "off"


def configure(env=None) -> dict:
    """Put the switches into ``env`` (the process environment by default) when the setting is off. Returns what was set, ``{}`` when the interface may use the card.
    A switch that is already there is kept, and the person's own flags are never removed."""
    env = os.environ if env is None else env
    if not wants_software(env):
        return {}
    flags = env.get("QTWEBENGINE_CHROMIUM_FLAGS", "").split()
    env["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(flags + [f for f in FLAGS_OFF if f not in flags])
    env.setdefault("QT_QUICK_BACKEND", "software")
    return {"QTWEBENGINE_CHROMIUM_FLAGS": env["QTWEBENGINE_CHROMIUM_FLAGS"], "QT_QUICK_BACKEND": env["QT_QUICK_BACKEND"]}
