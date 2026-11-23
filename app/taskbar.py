"""Flash the window's button on the Windows taskbar until the person brings the window forward (FlashWindowEx through ctypes).

Windows only: on another system, or when the call fails, nothing happens and the answer is False. It is a hint beside the notification, never required.
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

FLASHW_ALL = 0x00000003              # the caption and the taskbar button
FLASHW_TIMERNOFG = 0x0000000C        # keep flashing until the window is in front


class FLASHWINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("hwnd", wintypes.HWND), ("dwFlags", wintypes.DWORD), ("uCount", wintypes.UINT), ("dwTimeout", wintypes.DWORD)]


def flash(hwnd: int, *, user32=None) -> bool:
    """Start the flashing for the window ``hwnd``; True when Windows accepted the call."""
    if not hwnd:
        return False
    if user32 is None:
        if os.name != "nt":
            return False
        user32 = ctypes.windll.user32
    try:
        info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, FLASHW_ALL | FLASHW_TIMERNOFG, 0, 0)
        user32.FlashWindowEx(ctypes.byref(info))
        return True
    except Exception:  # ignored: a window that has gone, or a system without the call; the notification is the message, the flash only a hint
        return False
