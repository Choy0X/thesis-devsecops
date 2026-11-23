"""Which kind of window the desktop shell opens: its own frameless one with the page drawing the title bar, or the operating system's frame.

The default is the frameless window. It is kept only if a short self-test after the window opened agrees that it behaves: the frameless flag is on, the
page received the shell's bridge (so the title-bar buttons can work), and the window sits inside the display's work area. When any check fails the
window gets the system frame back at once (no restart), and the reasons are printed. ``ZTSA_WINDOW_FRAME=native`` (Settings > Developer) asks for the system
frame from the start.
"""
from __future__ import annotations

NATIVE = "native"


def wants_frameless(setting: str | None) -> bool:
    """False only for an explicit request for the system frame."""
    return (setting or "").strip().lower() != NATIVE


def evaluate(facts: dict) -> tuple[bool, list[str]]:
    """``facts``: ``frameless_flag`` (bool), ``bridge`` (bool: the page has the shell's bridge), ``inside_work_area`` (bool or None when unknown).
    Returns ``(keep the frameless window, reasons it should not be kept)``. An unknown work area is not a failure; a known mismatch is."""
    reasons = []
    if not facts.get("frameless_flag"):
        reasons.append("the window did not take the frameless setting")
    if not facts.get("bridge"):
        reasons.append("the page did not receive the shell's window bridge, so the title-bar buttons could not work")
    if facts.get("inside_work_area") is False:
        reasons.append("the window lies outside the display's work area")
    return (not reasons), reasons


def fit(window: tuple[int, int, int, int], area: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """The window rectangle moved (and shrunk when it is larger than the area) so that it lies inside the work area. A window that already fits
    is returned unchanged; one that sticks out is centred on the area when it is smaller, so a default position that was chosen for a taller display
    does not hide the bottom of the window under the taskbar."""
    wx, wy, ww, wh = window
    ax, ay, aw, ah = area
    if inside(window, area):
        return window
    ww, wh = min(ww, aw), min(wh, ah)
    return (ax + (aw - ww) // 2, ay + (ah - wh) // 2, ww, wh)


def inside(window: tuple[int, int, int, int], area: tuple[int, int, int, int], slack: int = 2) -> bool:
    """Whether a window rectangle (x, y, width, height) lies within the work area, allowing a few pixels for the frame's shadow."""
    wx, wy, ww, wh = window
    ax, ay, aw, ah = area
    return wx >= ax - slack and wy >= ay - slack and wx + ww <= ax + aw + slack and wy + wh <= ay + ah + slack
