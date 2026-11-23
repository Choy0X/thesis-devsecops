"""Text from outside the owner's own words (a project's notes file, a file attached with @) put in front of a model between markers, so that it is plainly data.

A marker is only worth anything if the text inside cannot close it. Two things keep it so: the marker carries a nonce that the text could not have known (it comes from the run, or from the
message the file is attached to), and the three-character runs the markers are made of are broken inside the text. What is inside may still say anything; it can no longer say where it ends.
"""
from __future__ import annotations

import hashlib
import re

_RUN = re.compile(r"<{3,}|>{3,}")


def nonce_of(*parts: str) -> str:
    """A short value that is the same for the same parts and not guessable from the text that is fenced (a message id and a run's own random value are not in the file)."""
    return hashlib.sha256("\x1f".join(parts).encode("utf-8", "replace")).hexdigest()[:10]


def neutralize(text: str) -> str:
    """``text`` with every run of three angle brackets broken, so that nothing inside it can look like the start or the end of a marker."""
    return _RUN.sub(lambda m: " ".join(m.group()), str(text))          # a run of any length: replacing "<<<" alone leaves a run behind when there are five


def wrap(label: str, body: str, nonce: str) -> str:
    """``body`` between ``<<<LABEL-nonce`` and ``LABEL-nonce>>>``."""
    return f"<<<{label}-{nonce}\n{neutralize(body)}\n{label}-{nonce}>>>"
