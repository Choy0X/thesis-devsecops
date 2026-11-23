"""Versioned JSON encoding of completed parser analyses, never parser objects."""
from dataclasses import asdict
import hashlib
from pathlib import Path
import sys

from .cache import default
from .metrics import current

_stack = None


def stack_fingerprint():
    global _stack
    if _stack is None:
        root = Path(__file__).resolve().parents[1]
        digest = hashlib.sha256()
        for name in ('funcsplit.py', 'splitmatch.py', 'splitdeps.py', 'astdiff.py'):
            digest.update((root / name).read_bytes())
        _stack = digest.hexdigest()
    return _stack


def encode(value):
    if isinstance(value, tuple):
        return {'$tuple': [encode(v) for v in value]}
    if isinstance(value, list):
        return [encode(v) for v in value]
    if isinstance(value, dict):
        return {'$dict': [[encode(k), encode(v)] for k, v in value.items()]}
    return value


def decode(value):
    if isinstance(value, list):
        return [decode(v) for v in value]
    if isinstance(value, dict):
        if '$tuple' in value:
            return tuple(decode(v) for v in value['$tuple'])
        return {decode(k): decode(v) for k, v in value['$dict']}
    return value


def analyze(fn, lang, path, before, after, diff):
    from app.backend.pipeline import funcsplit, splitmatch
    variant = 'tsx' if lang == 'typescript' and path.lower().endswith('.tsx') else ''
    key = [stack_fingerprint(), funcsplit.parser_fingerprint(), sys.version_info[:3],
           funcsplit._grammar_availability(lang, variant), lang, path,
           *(hashlib.sha256((s or '').encode()).hexdigest() for s in (before, after, diff))]
    def calculate():
        with current.timer('parse_and_match'):
            result = fn(lang, path, before, after, diff)
        if result.status != 'ok':
            raise NotCacheable(result)
        attributes = {k: v for k, v in vars(result).items() if k != 'matches'}
        return encode({'attributes': attributes, 'matches': [asdict(match) for match in result.matches]})
    try:
        value = decode(default().get_or_compute('analysis-v2', key, calculate))
        result = funcsplit.Analysis([], 'ok', None)
        result.__dict__.update(value['attributes'])
        result.matches = [splitmatch.Match(**row) for row in value['matches']]
        return result
    except NotCacheable as error:
        return error.analysis


class NotCacheable(Exception):
    def __init__(self, analysis):
        self.analysis = analysis
