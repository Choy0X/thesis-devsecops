"""Lightweight draft checks shared by generation and context selection."""
import difflib
import re

FIELDS = ("root_cause", "secure_pattern", "explanation")
MIN_WORDS = {"root_cause": 4, "secure_pattern": 4, "explanation": 6}
SAME_TEXT = 0.9

def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9_']+", text)


def _plain(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def _alike(a: str, b: str) -> bool:
    """The same text apart from case, punctuation and a few words."""
    x, y = _plain(a), _plain(b)
    return bool(x) and (x == y or difflib.SequenceMatcher(None, x, y).ratio() >= SAME_TEXT)


def check_draft(out: dict, p: dict) -> str | None:
    """Why a model-written draft says nothing worth keeping, or None. The faults seen with a small model: an empty field, a secure pattern that is only a function name, a root cause that copies the
    advisory title, and one sentence repeated in every field."""
    fields = {k: out.get(k) for k in FIELDS}
    for k, v in fields.items():
        if not isinstance(v, str) or not v.strip():
            return f"{k} is empty"
    pattern = fields["secure_pattern"].strip()
    if re.fullmatch(r"[^\s]+", pattern):
        return "secure_pattern is only a name, not a rule"
    for k, v in fields.items():
        if len(_words(v)) < MIN_WORDS[k]:
            return f"{k} is too short to say anything ({len(_words(v))} words)"
    if p.get("summary") and _alike(fields["root_cause"], p["summary"]):
        return "root_cause only repeats the advisory title"
    for a, b in (("root_cause", "secure_pattern"), ("root_cause", "explanation"), ("secure_pattern", "explanation")):
        if _alike(fields[a], fields[b]):
            return f"{a} and {b} are the same text"
    return None

