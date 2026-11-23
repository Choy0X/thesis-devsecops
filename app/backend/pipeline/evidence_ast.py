"""Pinned offline grammars for admission proofs, independent of mining parsers."""
from __future__ import annotations

import importlib.metadata
import threading

MAX_SOURCE_BYTES = 100000
_LOCAL = threading.local()


def grammar_signature():
    return ';'.join(name + '=' + importlib.metadata.version(distribution) for name, distribution in (
        ('core', 'tree-sitter'), ('javascript', 'tree-sitter-javascript'), ('typescript', 'tree-sitter-typescript')))


def parse(language, source):
    return _parse_bounded(language, source, MAX_SOURCE_BYTES)


def parse_snapshot(language, source):
    """Source recovery limit, independent of the trusted unit proof grammar."""
    from .security_snapshots import MAX_BLOB_BYTES
    return _parse_bounded(language, source, MAX_BLOB_BYTES)


def _parse_bounded(language, source, limit):
    language = {'js': 'javascript', 'jsx': 'javascript', 'ts': 'typescript', 'tsx': 'tsx'}.get(language, language)
    if language not in ('javascript', 'typescript', 'tsx') or not isinstance(source, str):
        return None
    data = source.encode('utf-8')
    if not data or len(data) > limit:
        return None
    parsers = getattr(_LOCAL, 'parsers', None)
    if parsers is None:
        parsers = _LOCAL.parsers = {}
    if language not in parsers:
        from tree_sitter import Language, Parser
        if language == 'javascript':
            from tree_sitter_javascript import language as grammar
        else:
            import tree_sitter_typescript
            grammar = tree_sitter_typescript.language_tsx if language == 'tsx' else tree_sitter_typescript.language_typescript
        parsers[language] = Parser(Language(grammar()))
    root = parsers[language].parse(data).root_node
    return None if root.has_error else (root, data)
