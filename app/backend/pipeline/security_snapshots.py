"""Exact-tree context inputs. Missing snapshots never become evidence of safety.

Only source paths selected by the context builder are read. Git is invoked without
a shell; network hydration is explicit and belongs to the collection job.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import subprocess
import posixpath
import threading
import sys
import weakref
import contextlib
import contextvars
from collections import OrderedDict
from functools import lru_cache
from dataclasses import dataclass
from types import SimpleNamespace
from urllib.parse import urlsplit

from app.backend.pipeline import context_builder as cb

MAX_BLOB_BYTES = 1 << 20
MAX_FILES = 64
_PREPARATION = contextvars.ContextVar('context_preparation', default=None)


def _unit_budget_safe(root, spec):
    """Prove an upper bound on overlapping extraction spans, or use the reference.

    Counting anonymous and duplicate wrappers is deliberately conservative.
    These can only cause reference extraction, never omit a budget failure.
    """
    if spec is None:
        return False
    stack, size, visited = [root], 0, 0
    while stack:
        node = stack.pop()
        visited += 1
        if visited > 100000:
            return False
        span = None
        if node.type in spec[1]:
            span = node
        elif node.type in ('variable_declarator', 'assignment_expression', 'pair'):
            value = node.child_by_field_name('right' if node.type == 'assignment_expression' else 'value')
            if value is not None and value.type in cb.funcsplit._JS_FUNC_VALUES:
                span = node
                parent = node.parent
                if parent is not None and ((node.type == 'variable_declarator' and parent.named_child_count == 1)
                        or (node.type == 'assignment_expression' and parent.type == 'expression_statement')):
                    span = parent
        if span is not None:
            size += span.end_byte - span.start_byte
            if size > cb.funcsplit.MAX_UNIT_CHARS:
                return False
        stack.extend(node.named_children)
    return True


def _caller_facts(language, path, source):
    """Cache primitive source-derived facts only, never native parser trees."""
    from app.backend.pipeline import splitdeps
    cache = _PREPARATION.get()
    key = (language, path, hashlib.sha256(source.encode()).digest())
    facts = cache.get(key) if cache is not None else None
    if facts is not None:
        return facts
    if cache is not None and cb.funcsplit._too_large(source) is None:
        # Import declarations are read directly from the same clean AST by
        # symbols_of; unit metadata only contributes function declarations.
        # Avoid splitting/hashing every function in non-caller files. Grammar
        # retries and syntax errors retain the complete reference path below.
        variant = 'tsx' if language == 'typescript' and path.lower().endswith('.tsx') else language
        spec = cb.funcsplit._TS.get(variant)
        grammar = spec[0] if spec else language
        parsed = cb.funcsplit._parse_for_edits(grammar, source)
        if parsed is not None and not parsed[0].has_error and _unit_budget_safe(parsed[0], spec):
            empty = cb.funcsplit._Units()
            empty.grammar = grammar
            symbols = splitdeps.symbols_of(cb.funcsplit.deps_source(language, empty, parsed[0], parsed[1]))
            facts = (grammar, None, tuple((d.source, d.member, d.name) for d in symbols.imports.values()))
            cache.put(key, facts)
            return facts
    units = cb._parsed_units(language, path, source)
    grammar = units.grammar or language
    parsed = cb.funcsplit._parse_for_edits(grammar, source)
    if parsed is None or parsed[0].has_error:
        return None
    symbols = splitdeps.symbols_of(cb.funcsplit.deps_source(language, units, parsed[0], parsed[1]))
    facts = (grammar, tuple(units.items()), tuple((d.source, d.member, d.name) for d in symbols.imports.values()))
    if cache is not None:
        cache.put(key, facts)
    return facts


class _CallerIndex:
    """Immutable path index with the reference same-directory-first ordering."""
    def __init__(self, paths):
        self.extensions = tuple(cb.JS_EXTS)
        self.ordered = tuple(sorted(p for p in paths if p.endswith(self.extensions)))
        self.known = frozenset(self.ordered)
        by_directory = {}
        for path in self.ordered:
            by_directory.setdefault(posixpath.dirname(path), []).append(path)
        self.by_directory = {directory: tuple(values) for directory, values in by_directory.items()}

    def select(self, path, limit):
        count = len(self.ordered) - int(path in self.known)
        if limit <= 0:
            return [], count > limit
        directory = posixpath.dirname(path)
        selected = []
        for candidate in self.by_directory.get(directory, ()):
            if candidate != path:
                selected.append(candidate)
                if len(selected) == limit:
                    return selected, count > limit
        for candidate in self.ordered:
            if candidate != path and posixpath.dirname(candidate) != directory:
                selected.append(candidate)
                if len(selected) == limit:
                    break
        return selected, count > limit


def caller_paths(snap, path):
    """Bounded, deterministic reverse-import search. Omitted files stay explicit."""
    if isinstance(snap, GitSnapshot):
        with snap._caller_guard:
            index = snap._caller_index
            if (index is None or snap._caller_source is not snap.paths or
                    index.extensions != tuple(cb.JS_EXTS)):
                index = snap._caller_index = _CallerIndex(snap.paths)
                snap._caller_source = snap.paths
        return index.select(path, MAX_FILES)
    # In-memory providers can change their path set; retain their reference behavior.
    candidates = sorted((p for p in snap.paths if p != path and p.endswith(cb.JS_EXTS)),
                        key=lambda p: (posixpath.dirname(p) != posixpath.dirname(path), p))
    return candidates[:MAX_FILES], len(candidates) > MAX_FILES


def _caller_binding_conflicts(root, data, references):
    """Exclude the imported declaration itself, retaining every other binder/write.

    CommonJS imports are variable declarators, so the generic function-local
    scanner counts their own names as locals. Only source-derived, top-level
    require declarations corresponding to the resolved imports are exempted.
    Scope precision stays conservative: a shadow anywhere suppresses that name.
    """
    from app.backend.pipeline import splitdeps
    allowed = set(references)
    imported_spans = set()
    required_names = set()
    for statement in root.named_children:
        if statement.type not in ('lexical_declaration', 'variable_declaration'):
            continue
        for declaration in statement.named_children:
            if declaration.type != 'variable_declarator':
                continue
            # Restrict the exemption to this declarator. A second declaration
            # of the same name in the same var statement is still a conflict.
            selected = SimpleNamespace(named_children=(declaration,),
                start_byte=statement.start_byte, start_point=statement.start_point)
            symbols = splitdeps.Symbols('ast')
            splitdeps._js_declarators(selected, data, symbols)
            imported = {d.name for d in symbols.imports.values()
                        if (d.source, d.member, d.name) in allowed}
            required_names.update(imported)
            pattern = declaration.child_by_field_name('name')
            found = []
            if pattern is not None:
                splitdeps._collect(pattern, splitdeps._JS, found)
            imported_spans.update((n.start_byte, n.end_byte) for n in found
                                  if splitdeps._text(n, data) in imported)
    conflicts, stack, visited = set(), [root], 0
    while stack:
        node = stack.pop()
        visited += 1
        if visited > splitdeps.MAX_SCAN_NODES:
            return conflicts, True
        if node.type in splitdeps._JS.prune:
            continue
        fields = splitdeps._JS.bind.get(node.type, ())
        if node.type in ('assignment_expression', 'augmented_assignment_expression'):
            fields = ('left',)
        elif node.type == 'update_expression':
            fields = ('argument',)
        for field in fields:
            children = (splitdeps._spec_children(node, '*') if field is None
                        else splitdeps._by_field(node, field))
            for child in children:
                found = []
                splitdeps._collect(child, splitdeps._JS, found)
                conflicts.update(splitdeps._text(n, data) for n in found
                                 if (n.start_byte, n.end_byte) not in imported_spans)
        stack.extend(node.named_children)
    if 'require' in conflicts:
        conflicts.update(required_names)
    return conflicts, False


def repository_callers(snap, path, name):
    from app.backend.pipeline import splitdeps
    if not name:
        return [], {}, []
    leaf = cb._leaf(name)
    paths, bounded = caller_paths(snap, path)
    items, files, omissions = [], {}, []
    if bounded:
        omissions.append('repository_caller_search_budget_exceeded')
    for candidate in paths:
        source = snap.read(candidate)
        if source is None:
            omissions.append('caller_source_unavailable:' + candidate)
            continue
        files[candidate] = source
        language = 'typescript' if candidate.endswith(('.ts', '.tsx')) else 'javascript'
        _, resolution, unresolved = compiler_resolution(snap, candidate, source)
        omissions.extend(unresolved)
        try:
            facts = _caller_facts(language, candidate, source)
        except cb.funcsplit.Unavailable:
            omissions.append('caller_source_unparsed:' + candidate)
            continue
        if facts is None:
            omissions.append('caller_source_unparsed:' + candidate)
            continue
        grammar, units, declarations = facts
        references = [declaration for declaration in declarations
                      if next((p for p in resolution.get(declaration[0], cb._js_candidates(candidate, declaration[0]))
                               if p in snap.paths), None) == path]
        if not references:
            continue
        names = set()
        for _, member, imported_name in references:
            if member == leaf or (name.split('#')[0] == 'module.exports' and member == ''):
                names.add(imported_name)
            elif member in ('*', ''):
                names.add(imported_name + '.' + leaf)
        if not names:
            omissions.append('caller_import_binding_unresolved:' + candidate)
            continue
        parsed = cb.funcsplit._parse_for_edits(grammar, source)
        if parsed is None or parsed[0].has_error:
            omissions.append('caller_source_unparsed:' + candidate)
            continue
        bound, truncated = _caller_binding_conflicts(parsed[0], parsed[1], references)
        if truncated:
            omissions.append('caller_binding_scan_budget:' + candidate)
            continue
        shadowed = {symbol for symbol in names if symbol.split('.')[0] in bound}
        if shadowed:
            omissions.append('caller_binding_shadowed_or_ambiguous:' + candidate)
        names -= shadowed
        if not names:
            continue
        if units is None:
            # Only actual candidate callers need complete function extraction.
            try:
                parsed_units = cb._parsed_units(language, candidate, source)
            except cb.funcsplit.Unavailable:
                omissions.append('caller_source_unparsed:' + candidate)
                continue
            grammar, units = parsed_units.grammar or language, tuple(parsed_units.items())
        if not units:
            # Executable modules can call an import from an anonymous IIFE.
            # The function splitter intentionally has no named unit for it.
            # Retain this bounded complete module, including its import binding.
            if len(source) <= cb.funcsplit.MAX_UNIT_CHARS:
                units = (('<module>', (source, 1)),)
            else:
                omissions.append('caller_module_budget_exceeded:' + candidate)
        for qualname, (text, _) in units:
            parsed_unit = cb.funcsplit._parse_for_edits(grammar, text)
            if parsed_unit is None:
                continue
            root, data = parsed_unit
            stack, calls = [root], set()
            while stack:
                node = stack.pop()
                if node.type == 'call_expression':
                    function = node.child_by_field_name('function')
                    if function is not None and function.type in ('identifier', 'member_expression'):
                        calls.add(data[function.start_byte:function.end_byte].decode('utf-8'))
                stack.extend(node.named_children)
            if names & calls:
                items.append(cb.ContextItem('caller', candidate, qualname, text,
                             'static call reference through a resolved import; execution is not verified'))
    omissions.append('dynamic_repository_callers_unresolved')
    return items, files, omissions


def logical_path(path: str) -> str:
    # A Git name is never a host filesystem path. Colons inside logical names
    # are legal; drive prefixes, traversal and command framing are not.
    if type(path) is str:
        parts = path.split('/')
        if (not path or path.startswith('/') or '..' in parts or
                any(c in path for c in ('\\', '\0', '\r', '\n')) or
                re.match(r'^[A-Za-z]:', path)):
            raise ValueError('invalid snapshot path')
        return '/'.join(part for part in parts if part and part != '.') or '.'
    # Subclasses retain the original constructor and string protocol behavior.
    if not isinstance(path, str):
        raise ValueError('invalid snapshot path')
    p = pathlib.PurePosixPath(path)
    if (not path or p.is_absolute() or '..' in p.parts or
            any(c in path for c in ('\\', '\0', '\r', '\n')) or
            re.match(r'^[A-Za-z]:', path)):
        raise ValueError('invalid snapshot path')
    return str(p)


def safe_path(path: str) -> str:
    value = logical_path(path)
    if ':' in value:
        raise ValueError('invalid filesystem snapshot path')
    return value


@dataclass
class Snapshot:
    revision: str | None
    files: dict
    available: bool = True

    def __post_init__(self):
        for path in self.files:
            logical_path(path)

    @property
    def paths(self):
        return frozenset(self.files)

    def read(self, path):
        return self.files.get(logical_path(path))


class GitSnapshot:
    def __init__(self, repository, revision):
        self.repository, self.revision = pathlib.Path(repository), revision
        self._caller_guard = threading.Lock()
        self._caller_index = self._caller_source = None
        self.available, self.paths = False, frozenset()
        self.service = None
        from app.backend.pipeline.acceleration import mining_policy
        if mining_policy.current().get('version') in (3, 4):
            from app.backend.pipeline.acceleration.repository_service import service, CompatibilityFallback
            try:
                self.service = service(repository)
                self.paths = self.service.tree(revision).keys()
                self.available = True
                self.read = self._shared_read
                return
            except (CompatibilityFallback, OSError, ValueError):
                self.service = None
        if not re.fullmatch(r'[0-9a-f]{40,64}', revision or ''):
            return
        try:
            rows = self._git('ls-tree', '-r', '-z', revision).split(b'\0')
            # Exclude symlinks and submodules; neither may resolve outside this tree.
            self.paths = frozenset(logical_path(r.split(b'\t', 1)[1].decode('utf-8')) for r in rows
                                   if r and r.startswith((b'100644 ', b'100755 ')))
            self.available = True
        except (OSError, ValueError, subprocess.SubprocessError):
            pass

    def _shared_read(self, path):
        operations_module = __import__('app.backend.pipeline.acceleration.operations', fromlist=['check'])
        operations_module.check(disk=False)
        try:
            data = self.service.read(self.revision, logical_path(path), limit=MAX_BLOB_BYTES)
            return data.decode('utf-8') if data is not None else None
        except operations_module.DeadlineExpired:
            raise
        except (OSError, ValueError, UnicodeError):
            return None

    def _git(self, *args):
        from app.backend.pipeline.acceleration import operations
        if operations.current():
            from app.backend.pipeline.ingest.codemine import _git
            return _git(['-C',str(self.repository),*args],text=False,timeout=30,no_lazy=True)
        return subprocess.run(['git', '-C', str(self.repository), *args], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
                              env={**os.environ, 'GIT_NO_LAZY_FETCH': '1', 'GIT_TERMINAL_PROMPT': '0'}).stdout

    @lru_cache(maxsize=256)
    def read(self, path):
        from app.backend.pipeline.acceleration import operations
        operations.check()
        path = logical_path(path)
        if path not in self.paths:
            return None
        try:
            ref = f'{self.revision}:{path}'
            from app.backend.pipeline.acceleration.git_objects import read
            content = read(self.repository, self.revision, path, limit=MAX_BLOB_BYTES)
            return content.decode('utf-8', errors='strict') if content is not None else None
        except operations.DeadlineExpired:
            raise
        except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
            return None


def repository_path(row):
    from app.backend.pipeline.ingest.codemine import REPOS
    parsed = urlsplit(row.get('repo_url') or '')
    parts = parsed.path.strip('/').removesuffix('.git').split('/')
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or len(parts) < 2:
        return None
    if not re.fullmatch(r'[A-Za-z0-9.-]+', parsed.hostname) or any(not re.fullmatch(r'[A-Za-z0-9_.-]+', p) or p in ('.', '..') for p in parts):
        return None
    return REPOS / parsed.hostname / pathlib.Path(*parts[:-1]) / (parts[-1] + '.git')


@lru_cache(maxsize=8)
def _git_snapshot(path, revision):
    return GitSnapshot(path, revision)


def snapshot_for(row, revision):
    path = repository_path(row)
    from app.backend.pipeline.acceleration import mining_policy
    factory = GitSnapshot if mining_policy.current().get('version') in (3, 4) else _git_snapshot
    base = factory(str(path), revision) if path and path.is_dir() else Snapshot(revision, {}, available=False)
    return acquired_fallback(base, row, revision)


class AcquiredSnapshot:
    """Read missing objects from committed exact-source acquisition only.

    Existing Git bytes win. Full acquired artifacts retain their notices and
    provenance; the provider does not create execution proof or replace refs.
    """
    def __init__(self, original, repository, revision, paths, root):
        self.original, self.repository, self.revision = original, repository, revision
        self.available = bool(original.available or paths)
        self.paths = frozenset(original.paths) | frozenset(paths)
        self.root = root

    def read(self, path):
        path = logical_path(path)
        value = self.original.read(path)
        if value is not None:
            return value
        from . import npm_evidence_sources, npm_case_collector
        value = None
        for module, name in ((npm_evidence_sources, '.npm_evidence_sources.sqlite3'),
                             (npm_case_collector, '.npm_case_collector.sqlite3')):
            if (self.root / name).is_file():
                value = module.read_exact(self.repository, self.revision, path, root=self.root)
                if value is not None:
                    break
        if not isinstance(value, str) or len(value.encode()) > MAX_BLOB_BYTES:
            return None
        return value


def acquired_fallback(original, row, revision):
    from . import versions
    root = pathlib.Path(versions.PROC).resolve()
    if not any((root / name).is_file() for name in ('.npm_evidence_sources.sqlite3', '.npm_case_collector.sqlite3')):
        return original
    from . import npm_evidence_sources, npm_case_collector
    repository = row.get('repo_url') or row.get('repository_identity') or ''
    try:
        npm_evidence_sources._repository(repository)
    except ValueError:
        return original
    if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{40}', revision):
        return original
    paths = set()
    for module, name in ((npm_evidence_sources, '.npm_evidence_sources.sqlite3'),
                         (npm_case_collector, '.npm_case_collector.sqlite3')):
        if (root / name).is_file():
            paths.update(module.exact_paths(repository, revision, root=root))
    return AcquiredSnapshot(original, repository, revision, paths, root) if paths else original


class HydrationSnapshots:
    """Keep at most the current exact before/after trees, never source misses.

    Only shared-service snapshots are reusable: their reads consult current Git
    availability rather than retaining the fallback reader's cached misses.
    A scope belongs to one context callback and is discarded after hydration.
    """
    def __init__(self):
        self._snapshots = OrderedDict()

    def __call__(self, row, revision):
        from app.backend.pipeline.acceleration import mining_policy
        if mining_policy.current().get('version') != 4 or not re.fullmatch(r'[0-9a-f]{40,64}', revision or ''):
            return snapshot_for(row, revision)
        path = repository_path(row)
        if path is None:
            return snapshot_for(row, revision)
        key = (str(path.resolve()), revision)
        snap = self._snapshots.pop(key, None)
        if snap is None:
            snap = snapshot_for(row, revision)
        if isinstance(snap, GitSnapshot) and snap.available and snap.service is not None:
            self._snapshots[key] = snap
            while len(self._snapshots) > 2:
                self._snapshots.popitem(last=False)
        return snap


def compiler_resolution(snap, path, text, *, reader=None):
    """Resolve declared TS paths only. Unsupported JSONC/extends stay explicit."""
    configs, resolution, omissions = {}, {}, []
    directories = [str(p) for p in pathlib.PurePosixPath(path).parents]
    candidates = [posixpath.normpath(posixpath.join(d, n)) for d in directories for n in ('tsconfig.json', 'jsconfig.json')]
    config_path = next((p for p in candidates if p in snap.paths), None)
    if not config_path:
        return configs, resolution, omissions
    content = (reader or snap.read)(config_path)
    if content is None:
        return configs, resolution, ['compiler_configuration_unfetched:' + config_path]
    configs[config_path] = content
    try:
        document = json.loads(content)
        options = document.get('compilerOptions') or {}
        if document.get('extends'):
            omissions.append('compiler_configuration_extends_unresolved:' + str(document['extends']))
        root = posixpath.normpath(posixpath.join(posixpath.dirname(config_path), options.get('baseUrl') or '.'))
        aliases = options.get('paths') or {}
        specs = [m.group(1) for m in cb._JS_IMPORT.finditer(text or '')] + [m.group(1) for m in cb._JS_REQUIRE.finditer(text or '')]
        for spec in specs:
            for pattern, replacements in aliases.items():
                capture = re.fullmatch(re.escape(pattern).replace(r'\*', '(.*)'), spec)
                if not capture or not isinstance(replacements, list):
                    continue
                for replacement in replacements:
                    stem = posixpath.normpath(posixpath.join(root, replacement.replace('*', capture.group(1) if capture.groups() else '')))
                    logical_path(stem)
                    candidates = cb._js_candidates('__root__.ts', './' + stem)
                    resolution.setdefault(spec, []).extend(candidates)
    except (ValueError, TypeError, AttributeError):
        omissions.append('compiler_configuration_unparsed:' + config_path)
    return configs, resolution, omissions


class _HydrationPlans:
    """One callback's immutable file/caller plans, with a combined hard bound.

    Strings already owned by admitted hydration inputs are borrowed, never
    copied. Charging them too is conservative. The per-entry allowance covers
    OrderedDict nodes/table growth and bookkeeping; 1 KiB stays for the store.
    """
    __slots__ = ('_entries', 'size', '__weakref__')
    _LIMIT_BYTES = (1 << 20) - 1024
    _LIMIT_ENTRIES = 128
    _ENTRY_BYTES = 512

    def __init__(self):
        self._entries, self.size = OrderedDict(), 0

    def __len__(self):
        return len(self._entries)

    def get(self, key):
        item = self._entries.pop(key, None)
        if item is not None:
            self._entries[key] = item
            return item[0]

    @classmethod
    def _cost(cls, value):
        # Repeated references are charged repeatedly too. This avoids a large
        # temporary visited set and covers tuple copies made while freezing.
        size = sys.getsizeof(value)
        if isinstance(value, (tuple, list)):
            for part in value:
                size += cls._cost(part)
                if size > cls._LIMIT_BYTES:
                    break
        return size

    @classmethod
    def _freeze(cls, value):
        if isinstance(value, (tuple, list)):
            return tuple(cls._freeze(part) for part in value)
        return value

    def put(self, key, value):
        old = self._entries.pop(key, None)
        if old is not None:
            self.size -= old[1]
        size = self._ENTRY_BYTES + self._cost(key) + self._cost(value)
        if size > self._LIMIT_BYTES:
            return
        while self._entries and (len(self._entries) >= self._LIMIT_ENTRIES or
                                 self.size + size > self._LIMIT_BYTES):
            _, (_, removed) = self._entries.popitem(last=False)
            self.size -= removed
        self._entries[key] = (self._freeze(value), size)
        self.size += size


class _PreparationFacts(_HydrationPlans):
    _LIMIT_BYTES = 8 * (1 << 20)
    _LIMIT_ENTRIES = 256


@contextlib.contextmanager
def preparation():
    """One bounded export's caller facts; discarded on success or failure."""
    existing = _PREPARATION.get()
    if existing is not None:
        yield existing
        return
    cache = _PreparationFacts()
    token = _PREPARATION.set(cache)
    try:
        yield cache
    finally:
        _PREPARATION.reset(token)


def hydrate_pairs(rows, commit, *, provider=snapshot_for, max_files=128, max_bytes=64 << 20):
    """Collection-only lazy fetch into the repository cache, with a durable omission report.

    Never run from dataset building or from a student prompt. A byte/file cap leaves
    evidence unavailable for strict review instead of cutting a source file.
    """
    from app.backend.pipeline.acceleration import operations
    from app.backend.pipeline.acceleration import mining_policy
    if provider is snapshot_for and mining_policy.current().get('version') == 4:
        provider = HydrationSnapshots()
    seen, cache, fetched, missing, total = set(), {}, [], [], 0
    plans = _HydrationPlans() if mining_policy.current().get('version') == 4 else None
    for row in rows:
        if row.get('language') not in cb.JS_LANGS:
            continue
        for side, revision in [('before', row.get('parent_commit')), ('after', row.get('fix_commit'))]:
            if not re.fullmatch(r'[0-9a-f]{40,64}', revision or ''):
                missing.append({'side': side, 'reason': 'missing_exact_revision'}); continue
            snap = provider(row, revision)
            path = logical_path((row.get('old_path') if side == 'before' else None) or row['file_path'])
            prepared = set()
            def prefetch(candidates):
                method = getattr(commit.loader, 'prefetch', None)
                if method is None:
                    return
                remaining = max(0, max_files - len(fetched))
                paths = list(dict.fromkeys(p for p in candidates if p in snap.paths
                                          and (revision, p) not in seen and p not in prepared))[:remaining]
                if paths:
                    method([(revision, p) for p in paths])
                    prepared.update(paths)
            # Read only known files. The loader persists git's lazily fetched blobs.
            def read(candidate):
                nonlocal total
                operations.phase('hydrating_context', processed=len(fetched), total=max_files, unit='files (budget)')
                key = (revision, candidate)
                if key in seen or candidate not in snap.paths:
                    return cache.get(key)
                seen.add(key)
                if len(fetched) >= max_files or total >= max_bytes:
                    missing.append({'revision': revision, 'path': candidate, 'reason': 'hydration_budget'}); return None
                if candidate not in prepared:
                    # A rejected/non-UTF8 object can leave a slot for a later path.
                    # Refill it explicitly without changing admission order.
                    method = getattr(commit.loader, 'prefetch', None)
                    if method is not None:
                        method([(revision, candidate)])
                        prepared.add(candidate)
                data = commit.loader.content(revision, candidate)
                if data is None or len(data) > MAX_BLOB_BYTES or total + len(data) > max_bytes:
                    missing.append({'revision': revision, 'path': candidate, 'reason': 'unavailable_or_over_budget'}); return None
                try: text = data.decode('utf-8')
                except UnicodeError:
                    missing.append({'revision': revision, 'path': candidate, 'reason': 'non_utf8_source'}); return None
                total += len(data); fetched.append({'revision': revision, 'path': candidate, 'bytes': len(data)})
                cache[key] = text
                return text
            configurations = [posixpath.normpath(posixpath.join(str(directory), name))
                              for directory in pathlib.PurePosixPath(path).parents for name in ('tsconfig.json', 'jsconfig.json')]
            prefetch([path, *configurations])
            target = read(path) or row.get('code_' + side) or ''
            for directory in pathlib.PurePosixPath(path).parents:
                for name in ('tsconfig.json', 'jsconfig.json'):
                    read(posixpath.normpath(posixpath.join(str(directory), name)))
            from app.backend.pipeline.acceleration import mining_policy
            admitted_reader = (lambda candidate: cache.get((revision, candidate))) if mining_policy.current().get('version') == 4 else None
            # Reuse only pure plans for this exact shared snapshot. A weak
            # identity cannot extend the provider's two-tree lifetime. The path
            # binding also separates a replaced listing on the same instance.
            admitted_target = cache.get((revision, path))
            cacheable = (plans is not None and admitted_reader is not None and
                         isinstance(snap, GitSnapshot) and snap.available and snap.service is not None and
                         bool(admitted_target))
            config_path = admitted_config = None
            if cacheable:
                config_path = next((candidate for candidate in configurations if candidate in snap.paths), None)
                admitted_config = cache.get((revision, config_path)) if config_path is not None else None
                cacheable = config_path is None or admitted_config is not None
            file_key = identity = None
            wanted = None
            if cacheable:
                identity = (weakref.ref(snap), str(snap.repository), snap.revision, id(snap.paths))
                adapter = cb.ecosystems.get(row.get('ecosystem'))
                manifests = tuple(adapter.manifest_files) if adapter is not None else ()
                planning = (cb.POLICY, tuple(cb.JS_EXTS), cb._JS_IMPORT.pattern, cb._JS_IMPORT.flags,
                            cb._JS_REQUIRE.pattern, cb._JS_REQUIRE.flags, cb._LOCKISH.pattern,
                            cb._LOCKISH.flags, manifests)
                file_key = ('file', identity, path, admitted_target, row.get('language'), row.get('ecosystem'),
                            config_path, admitted_config, planning)
                wanted = plans.get(file_key)
            if wanted is None:
                _, resolution, config_omissions = compiler_resolution(snap, path, target, reader=admitted_reader)
                unit = cb.Unit(path=path, code=row.get('code_' + side) or '', file_text=target, language=row.get('language'),
                               ecosystem=row.get('ecosystem'), known_paths=snap.paths, resolution=resolution)
                wanted = cb.wanted_paths(unit)
                del unit  # Plans must not retain a Unit that owns an older tree view.
                cacheable = cacheable and not any(reason.startswith(('compiler_configuration_unparsed:',
                    'compiler_configuration_unfetched:')) for reason in config_omissions)
                if cacheable:
                    plans.put(file_key, wanted)
            callers, caller_configs = [], []
            if row.get('function_name'):
                caller_key = ('callers', identity, path, tuple(cb.JS_EXTS), MAX_FILES) if cacheable else None
                caller_plan = plans.get(caller_key) if cacheable else None
                if caller_plan is None:
                    callers = caller_paths(snap, path)[0]
                    caller_configs = [posixpath.normpath(posixpath.join(str(directory), name))
                                      for candidate in callers for directory in pathlib.PurePosixPath(candidate).parents
                                      for name in ('tsconfig.json', 'jsconfig.json')]
                    if cacheable:
                        plans.put(caller_key, (callers, caller_configs))
                else:
                    callers, caller_configs = caller_plan
            prefetch([*wanted, *callers, *caller_configs])
            for candidate in wanted:
                read(candidate)
            if row.get('function_name'):
                for candidate in callers:
                    read(candidate)
                    for directory in pathlib.PurePosixPath(candidate).parents:
                        for name in ('tsconfig.json', 'jsconfig.json'):
                            read(posixpath.normpath(posixpath.join(str(directory), name)))
    # Availability changes when lazy blobs arrive, even though successful content
    # at a pinned commit is immutable. Never retain a pre-hydration cache miss.
    from app.backend.pipeline.acceleration import mining_policy
    if mining_policy.current().get('version') not in (3, 4):
        _git_snapshot.cache_clear()
        GitSnapshot.read.cache_clear()
    return {'schema': 'snapshot-hydration/2' if mining_policy.current().get('version') == 4 else 'snapshot-hydration/1', 'fetched': len(fetched), 'bytes': total, 'files': fetched, 'omissions': missing}


def build_pair(row, *, before_text=None, after_text=None, provider=snapshot_for, budget=None):
    budget = budget or cb.Budget()
    result = {}
    for side, revision, fallback in [('before', row.get('parent_commit'), before_text),
                                     ('after', row.get('fix_commit'), after_text)]:
        snap = provider(row, revision)
        path = (row.get('old_path') if side == 'before' else None) or row.get('file_path') or 'unknown'
        code = row.get('code_' + side) or ''
        file_text = snap.read(path) if snap.available else None
        file_text = file_text if file_text is not None else fallback
        if file_text is None and row.get('granularity') == 'file':
            file_text = code
        unit = cb.Unit(path=path, code=code, file_text=file_text, language=row.get('language'),
                       ecosystem=row.get('ecosystem'), name=row.get('function_name') if row.get('granularity') == 'function' else None,
                       known_paths=snap.paths if snap.available else None)
        configs, resolution, config_omissions = compiler_resolution(snap, path, file_text) if row.get('language') in cb.JS_LANGS else ({}, {}, [])
        unit = cb.Unit(**{**unit.__dict__, 'resolution': resolution})
        blobs = dict(configs)
        wanted = cb.wanted_paths(unit)
        # Alias configuration is retained as evidence even when a dynamic import
        # cannot be resolved. The omission remains visible instead of guessing.
        for candidate in dict.fromkeys([*wanted, 'package.json', 'tsconfig.json', 'jsconfig.json']):
            if len(blobs) >= MAX_FILES:
                break
            text = snap.read(candidate) if snap.available else None
            if text is not None:
                blobs[candidate] = text
        unit = cb.Unit(**{**unit.__dict__, 'blobs': blobs})
        bundle = cb.build(unit, budget)
        bundle.omissions.extend(config_omissions)
        if row.get('language') in cb.JS_LANGS and row.get('function_name'):
            callers, caller_files, caller_omissions = repository_callers(snap, path, row['function_name'])
            blobs.update(caller_files)
            remaining = max(0, budget.total - sum(len(item.text) for item in bundle.items))
            caller_budget = max(0, budget.callers - sum(len(item.text) for item in bundle.items if item.kind == 'caller'))
            kept, _, trimmed = cb._fit([(i.kind, i.path, i.name, i.text, i.reason) for i in callers], caller_budget, remaining)
            bundle.items.extend(kept)
            bundle.omissions.extend(caller_omissions)
            if trimmed:
                bundle.omissions.append('budget_trimmed:repository_callers')
        if row.get('language') in cb.JS_LANGS:
            specs = [m.group(1) for m in cb._JS_IMPORT.finditer(file_text or '')] + [m.group(1) for m in cb._JS_REQUIRE.finditer(file_text or '')]
            for spec in sorted(set(specs)):
                if not spec.startswith(('./', '../')) and spec not in resolution:
                    bundle.omissions.append('external_import_unresolved:' + spec)
        if snap.available and file_text is not None and code.strip() and code.strip() not in file_text:
            bundle.omissions.append('target_source_mismatch')
        if not snap.available:
            bundle.omissions.append('repository_snapshot_unavailable')
        if len(wanted) > MAX_FILES:
            bundle.omissions.append('snapshot_file_budget_exceeded')
        bundle.completeness = 'partial' if bundle.omissions else 'complete'
        bundle.snapshot = {'revision': revision, 'side': side, 'path': path,
                           'available': snap.available, 'files': {p: hashlib.sha256(t.encode()).hexdigest() for p, t in sorted(blobs.items())},
                           'target_sha256': hashlib.sha256(code.encode()).hexdigest(),
                           'file_sha256': hashlib.sha256(file_text.encode()).hexdigest() if file_text is not None else None}
        result[side] = bundle
    return result
