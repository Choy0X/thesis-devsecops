"""Remove credentials from text that is about to be stored or printed.

An exception message can carry the value of a header or a URL that held a secret (the first Linux campaign run stored ``Bearer github_pat_...`` in five files because ``requests`` repeats
the header value in its InvalidHeader message). Every place that records a failure text (the commit history, the pipeline spool, the fix-commit table, the job log lines) passes it through
``text()`` first. Known token shapes are replaced, and so is the exact value of any credential environment variable of this process, whatever its shape.
"""
from __future__ import annotations

import os
import re

MARK = '<redacted>'
ENV_NAMES = ('GITHUB_TOKEN', 'GH_TOKEN', 'HF_TOKEN', 'HUGGING_FACE_HUB_TOKEN', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY')
PATTERNS = (
    re.compile(r'github_pat_[A-Za-z0-9_]{20,}'),
    re.compile(r'\bgh[pousr]_[A-Za-z0-9]{20,}'),
    re.compile(r'\bhf_[A-Za-z0-9]{20,}'),
    re.compile(r'\bsk-[A-Za-z0-9_\-]{20,}'),
    re.compile(r'(?i)\b(bearer|token)\s+[A-Za-z0-9._~+/=\-]{16,}'),
    re.compile(r'(?i)(authorization|x-api-key|api[_-]?key|password|passwd|secret)(["\']?\s*[:=]\s*["\']?)[^\s"\',;)]{8,}'),
    re.compile(r'(?<=://)[^/\s:@]+:[^/\s@]+(?=@)'),                  # user:password@host in a URL
)


def text(value) -> str:
    """The text with known credentials replaced by a marker. Never raises; a non-string is converted first."""
    try:
        out = value if isinstance(value, str) else str(value)
    except Exception:                   # ignored: a value whose str() fails is fully redacted, because text() never raises
        return MARK
    for name in ENV_NAMES:
        secret = (os.environ.get(name) or '').strip()
        if len(secret) >= 8:
            out = out.replace(secret, MARK)
    for pattern in PATTERNS:
        if pattern.groups == 2:
            out = pattern.sub(lambda m: m.group(1) + m.group(2) + MARK, out)
        elif pattern.groups == 1:
            out = pattern.sub(lambda m: m.group(1) + ' ' + MARK, out)
        else:
            out = pattern.sub(MARK, out)
    return out
