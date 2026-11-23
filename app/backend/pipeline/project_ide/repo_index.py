"""Local whole-file ranking. Cached names locate evidence, which is reread from source."""
from __future__ import annotations

import ast
from contextlib import closing
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from queue import SimpleQueue
import re
import sqlite3
import stat
import threading
import time

from . import workspace
from .chat_context import _safe_path
from .resources import Cancelled
from .repo_search import (MAX_FILE_BYTES, MAX_TOTAL_BYTES, SearchBudget, SearchHit, SearchResponse, SearchScope,
                          _match_files, changed_sources, inventory, literal_hits, normalize_path, read_source,
                          source_signature)

_DB_NAME = "search-v1.sqlite3"
_LANGUAGES = {".py": "python", ".js": "javascript", ".jsx": "javascript", ".ts": "typescript", ".tsx": "typescript",
              ".java": "java", ".go": "go", ".rs": "rust", ".c": "c", ".h": "c"}
_STOPWORDS = {"a", "an", "and", "are", "be", "can", "do", "does", "for", "from", "how", "in", "is", "it", "of", "on",
              "or", "that", "the", "this", "to", "what", "when", "where", "which", "with"}
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_IDENTIFIER = re.compile(r"[\w$]+(?:[.:][\w$]+)*")
_SYMBOL_LIMIT = 160
_SYNTAX_SECONDS = 0.12
_REFRESH_INTERVAL = 5.0
_LIVE_SECONDS = 0.18
_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(scope TEXT PRIMARY KEY, checked REAL NOT NULL, omissions TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, signature TEXT NOT NULL,
                                 size INTEGER NOT NULL, syntax_note TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(path UNINDEXED, path_terms, identifiers, body, tokenize='unicode61');
CREATE TABLE IF NOT EXISTS symbols(path TEXT NOT NULL, name TEXT NOT NULL, qual TEXT NOT NULL,
                                   module TEXT NOT NULL, member TEXT NOT NULL, kind TEXT NOT NULL,
                                   start_line INTEGER NOT NULL, end_line INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS symbols_path ON symbols(path, kind);
CREATE INDEX IF NOT EXISTS symbols_def ON symbols(kind, name, qual);
"""


# Files indexed per committed transaction, so a build that runs out of time keeps its progress.
_BATCH = 200
_CONTINUES = "The search index is still being built; the next search continues it."


@dataclass(frozen=True)
class IndexStatus:
    state: str
    indexed_files: int
    omissions: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class FileCandidate:
    path: str
    sha256: str
    match_kind: str
    start_line: int = 0
    end_line: int = 0


@dataclass(frozen=True)
class _Symbol:
    name: str
    qual: str
    module: str
    member: str
    kind: str
    start_line: int
    end_line: int


class _IndexState:
    def __init__(self):
        self.condition = threading.Condition()
        self.running = False
        self.last: IndexStatus | None = None
        self.stale: set[str] = set()


_guard = threading.Lock()
_states: dict[Path, _IndexState] = {}
_workers: dict[Path, tuple[threading.Event, threading.Thread]] = {}
_worker_slots = threading.BoundedSemaphore(2)
_syntax_slot = threading.BoundedSemaphore(1)
_syntax_queue = SimpleQueue()
_syntax_thread: threading.Thread | None = None


def _state(scope: SearchScope) -> _IndexState:
    with _guard:
        state = _states.get(scope.cache_dir)
        if state is None:
            state = _states[scope.cache_dir] = _IndexState()
        return state


def _cache_file(scope: SearchScope, *, create: bool = False) -> Path:
    database = scope.cache_dir / _DB_NAME
    for path in database.parents:
        try:
            if workspace.linked(path):
                raise ValueError("Linked cache paths are excluded.")
        except FileNotFoundError:
            continue
    for suffix in ("", "-wal", "-shm", "-journal"):
        path = database.with_name(database.name + suffix)
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError("The search cache must use app-owned regular files.")
    if create:
        database.parent.mkdir(parents=True, exist_ok=True)
    return database


def _scope_hash(scope: SearchScope) -> str:
    return hashlib.sha256(os.fsencode(os.path.normcase(scope.root))).hexdigest()


def _meta(db, scope: SearchScope):
    row = db.execute("SELECT scope, checked, omissions FROM meta").fetchone()
    if row is not None and row[0] != _scope_hash(scope):
        raise ValueError("The search cache belongs to another source tree.")
    return row


def _read_db(scope: SearchScope):
    database = _cache_file(scope)
    return sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0.08)


def _words(text: str) -> list[str]:
    return [piece.casefold() for word in _WORD.findall(text) for piece in _CAMEL.split(word) if piece]


def _terms(terms: list[str]) -> tuple[list[str], list[str]]:
    if (not isinstance(terms, list) or not 1 <= len(terms) <= 6
            or any(not isinstance(term, str) or not term.strip() or len(term) > 300
                   or any(mark in term for mark in "\0\r\n") for term in terms)):
        raise ValueError("Use 1 to 6 search terms of at most 300 characters each.")
    names = list(dict.fromkeys(term.strip().strip("'\"`") for term in terms if term.strip().strip("'\"`")))
    tokens = list(dict.fromkeys(word for name in names for word in _words(name) if word not in _STOPWORDS))
    return names, tokens[:24]


def _module_path(relative: str) -> str:
    path = PurePosixPath(relative).with_suffix("")
    if path.name in {"__init__", "index"}:
        path = path.parent
    return ".".join(path.parts)


def _python_symbols(relative: str, text: str) -> list[_Symbol] | None:
    """Use the built-in parser when the optional Python grammar is not cached."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return None
    module = _module_path(relative)
    pending = [(tree, "")]
    symbols = []
    while pending and len(symbols) < _SYMBOL_LIMIT:
        node, base = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            base = ".".join(filter(None, (base, node.name)))
            qual = ".".join(filter(None, (module, base)))
            symbols.append(_Symbol(node.name, qual, module, "", "definition", node.lineno, node.end_lineno))
        elif isinstance(node, ast.ImportFrom):
            symbols.extend(_Symbol(alias.asname or alias.name, "", "." * node.level + (node.module or ""),
                                   alias.name, "reference", node.lineno, node.end_lineno) for alias in node.names)
        elif isinstance(node, ast.Import):
            symbols.extend(_Symbol(alias.asname or alias.name, "", alias.name, "", "reference", node.lineno,
                                   node.end_lineno) for alias in node.names)
        pending.extend((child, base) for child in reversed(list(ast.iter_child_nodes(node))))
    return symbols[:_SYMBOL_LIMIT]


def _offline_grammars() -> set[str]:
    # get_parser can download a grammar. Only use cached grammars, or JS/TS
    # for which funcsplit uses the dedicated bundled wheels.
    try:
        import tree_sitter_language_pack
        return set(tree_sitter_language_pack.downloaded_languages())
    except (ImportError, OSError, RuntimeError):
        return set()


def _extract_symbols(relative: str, text: str, installed: set[str]) -> list[_Symbol] | None:
    language = _LANGUAGES.get(PurePosixPath(relative).suffix.casefold())
    if language == "python" and language not in installed:
        return _python_symbols(relative, text)
    if language not in (installed | {"javascript", "typescript"}):
        return None
    from app.backend.pipeline import splitdeps
    structure = splitdeps.file_symbols(language, relative, text)
    if structure is None:
        return None
    module = structure.module or _module_path(relative)
    definitions = [_Symbol(info.name, ".".join(filter(None, (module, info.qual))), module, "", "definition", *info.lines)
                   for functions in structure.functions.values() for info in functions]
    definitions.extend(_Symbol(item.name, f"{module}.{item.name}", module, "", "definition", item.line, item.line)
                       for item in list(structure.types.values()) + list(structure.constants.values()) if item.line > 0)
    references = [_Symbol(item.name, "", item.source, item.member, "reference", item.line, item.line)
                  for item in structure.import_order if item.source and item.line > 0]
    return (sorted(definitions, key=lambda item: item.start_line) + references)[:_SYMBOL_LIMIT]


def _bounded_symbols(relative: str, text: str, installed: set[str]) -> tuple[list[_Symbol], str]:
    global _syntax_thread
    if PurePosixPath(relative).suffix.casefold() not in _LANGUAGES:
        return [], ""
    finished = threading.Event()
    result = []

    def run():
        try:
            result.append(_extract_symbols(relative, text, installed))
        except Exception:                   # ignored: a parser failure is recorded as None and reported as 'no available syntax parser'
            result.append(None)
        finally:
            _syntax_slot.release()
            finished.set()

    timed_out = "Some syntax analysis timed out. Keyword search remains available."
    deadline = time.monotonic() + _SYNTAX_SECONDS
    if not _syntax_slot.acquire(timeout=_SYNTAX_SECONDS):
        return [], timed_out
    with _guard:
        if _syntax_thread is None:
            def parse():
                while True:
                    _syntax_queue.get()()

            _syntax_thread = threading.Thread(target=parse, name="project-search-symbols", daemon=True)
            _syntax_thread.start()
    _syntax_queue.put(run)
    if not finished.wait(max(0, deadline - time.monotonic())):
        return [], timed_out
    if not result or result[0] is None:
        return [], "Some files have no available syntax parser. Keyword search remains available."
    return result[0], ""


def _remove_rows(db, path: str):
    db.execute("DELETE FROM search WHERE path = ?", (path,))
    db.execute("DELETE FROM symbols WHERE path = ?", (path,))
    db.execute("DELETE FROM files WHERE path = ?", (path,))


def _is_corrupt(error: sqlite3.DatabaseError) -> bool:
    return getattr(error, "sqlite_errorcode", 0) & 0xFF in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}


def _clear_cache_files(database: Path):
    for suffix in ("-wal", "-shm", "-journal", ""):
        path = database.with_name(database.name + suffix)
        path.unlink(missing_ok=True)


class RepoIndex:
    def status(self, scope: SearchScope) -> IndexStatus:
        try:
            if not _cache_file(scope).is_file():
                return IndexStatus("missing", 0, [])
            with closing(_read_db(scope)) as db:
                meta = _meta(db, scope)
                if meta is None:
                    return IndexStatus("missing", 0, [])
                count = db.execute("SELECT count(*) FROM files").fetchone()[0]
            return IndexStatus("ready", count, json.loads(meta[2]))
        except (OSError, ValueError, sqlite3.Error):
            return IndexStatus("unavailable", 0, ["Local search cache is unavailable."])

    def refresh(self, scope: SearchScope, *, cancelled=None) -> IndexStatus:
        guard = SearchBudget(120.0, cancelled)
        guard.check()
        state = _state(scope)
        with state.condition:
            if state.running:
                while state.running:
                    if not guard.check():
                        return IndexStatus("building", 0, sorted(guard.omissions))
                    state.condition.wait(0.08)
                return state.last or self.status(scope)
            state.running = True
            stale = set(state.stale)
        result = None
        try:
            database = _cache_file(scope, create=True)
            try:
                result = self._refresh(scope, database, guard, stale)
            except sqlite3.DatabaseError as error:
                if not _is_corrupt(error):
                    raise
                guard.check()
                _clear_cache_files(database)
                result = self._refresh(scope, database, guard, stale)
            return result
        except (OSError, ValueError, sqlite3.Error):
            result = IndexStatus("unavailable", 0, ["Local search cache could not be built."])
            return result
        finally:
            with state.condition:
                if result is not None and result.state == "ready":
                    state.stale.difference_update(stale)
                state.last, state.running = result, False
                state.condition.notify_all()

    def _refresh(self, scope: SearchScope, database: Path, budget: SearchBudget, stale: set[str]) -> IndexStatus:
        names = inventory(scope.root, budget, managed_root=scope.managed_root)
        if not budget.check():
            return IndexStatus("unavailable", 0, sorted(budget.omissions))
        installed = _offline_grammars()
        with closing(sqlite3.connect(database, timeout=0.2)) as db:
            db.executescript(_SCHEMA)
            _meta(db, scope)
            records = {path: (signature, note) for path, signature, note in
                       db.execute("SELECT path, signature, syntax_note FROM files")}
            seen_dirs, retained = set(), set()
            total_bytes = written = 0
            try:
                for path in names:
                    if not budget.check():
                        # Keep what this refresh indexed; the next refresh continues from it (no meta row yet,
                        # so readers do not treat the cache as complete).
                        db.commit()
                        return IndexStatus("building", len(retained), sorted(budget.omissions | {_CONTINUES}))
                    try:
                        signature, size = source_signature(scope.root, path, seen_dirs)
                    except (OSError, ValueError):
                        continue
                    if size > MAX_FILE_BYTES or total_bytes + size > MAX_TOTAL_BYTES:
                        budget.omissions.add("Source size limit reached.")
                        continue
                    existing = records.get(path)
                    if existing and existing[0] == signature and path not in stale and "timed out" not in existing[1]:
                        retained.add(path)
                        total_bytes += size
                        if existing[1]:
                            budget.omissions.add(existing[1])
                        continue
                    source = read_source(scope.root, path, budget)
                    if source is None:
                        continue
                    symbols, note = _bounded_symbols(path, source.text, installed)
                    if note:
                        budget.omissions.add(note)
                    if existing:
                        _remove_rows(db, path)
                    db.execute("INSERT INTO files VALUES (?, ?, ?, ?, ?)", (path, source.sha256, source.signature, size, note))
                    identifiers = " ".join(" ".join(_words(item.name + " " + item.qual)) for item in symbols)
                    db.execute("INSERT INTO search VALUES (?, ?, ?, ?)",
                               (path, " ".join(_words(path)), identifiers, " ".join(_words(source.text))))
                    db.executemany("INSERT INTO symbols VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                   ((path, item.name.casefold(), item.qual.casefold(), item.module, item.member,
                                     item.kind, item.start_line, item.end_line) for item in symbols))
                    retained.add(path)
                    total_bytes += size
                    written += 1
                    if written % _BATCH == 0:
                        db.commit()
                if not budget.check():
                    db.commit()
                    return IndexStatus("building", len(retained), sorted(budget.omissions | {_CONTINUES}))
                for path in records.keys() - retained:
                    _remove_rows(db, path)
                db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?, ?)",
                           (_scope_hash(scope), time.time(), json.dumps(sorted(budget.omissions))))
                db.commit()
            except BaseException:
                db.rollback()
                raise
        return IndexStatus("ready", len(retained), sorted(budget.omissions))

    def rank(self, scope: SearchScope, terms: list[str], *, limit: int = 12) -> list[FileCandidate]:
        names, words = _terms(terms)
        if not 1 <= limit <= 40:
            raise ValueError("Use 1 to 40 file candidates.")
        if not words:
            return []
        try:
            with closing(_read_db(scope)) as db:
                if _meta(db, scope) is None:
                    return []
                queries = [name.casefold() for name in names if name and not any(char.isspace() for char in name)]
                definitions = []
                if queries:
                    # Place full qualified matches before same-name declarations.
                    marks = ",".join("?" for _ in queries)
                    definitions = db.execute(
                        f"SELECT s.path, f.sha256, s.start_line, s.end_line FROM symbols s JOIN files f ON f.path=s.path "
                        f"WHERE s.kind='definition' AND (s.qual IN ({marks}) OR s.name IN ({marks})) "
                        f"ORDER BY (s.qual IN ({marks})) DESC, s.path, s.start_line LIMIT ?",
                        (*queries, *queries, *queries, limit)).fetchall()
                candidates = {path: FileCandidate(path, digest, "definition", start, end)
                              for path, digest, start, end in reversed(definitions)}
                # The definition order is part of the public ranking.
                order = list(dict.fromkeys(row[0] for row in definitions))
                expression = " OR ".join(f'"{word}"' for word in words)
                phrases = [_words(name) for name in names]
                phrase_expression = " OR ".join(f'"{" ".join(phrase)}"' for phrase in phrases if len(phrase) > 1)
                matched_phrases = []
                select = ("SELECT search.path, files.sha256 FROM search JOIN files ON search.path=files.path "
                          "WHERE search MATCH ? ORDER BY bm25(search, 0.0, 5.0, 3.0, 1.0), search.path LIMIT ?")
                if phrase_expression:
                    matched_phrases = db.execute(select, (phrase_expression, limit)).fetchall()
                lexical = db.execute(select, (expression, limit)).fetchall()
            for path, digest in (*matched_phrases, *lexical):
                if path not in candidates:
                    order.append(path)
                    candidates[path] = FileCandidate(path, digest, "lexical")
            return [candidates[path] for path in order[:limit]]
        except (OSError, ValueError, sqlite3.Error):
            return []

    def search_changes(self, scope: SearchScope, needles: list[str], *, limit: int, budget: SearchBudget) -> list[SearchHit]:
        live = SearchBudget(min(_LIVE_SECONDS, max(0, budget.deadline - time.monotonic())), budget.cancelled)
        live.bytes_read = budget.bytes_read
        try:
            with closing(_read_db(scope)) as db:
                if _meta(db, scope) is None:
                    return []
                indexed = dict(db.execute("SELECT path, signature FROM files"))
            result = _match_files(scope, changed_sources(scope.root, indexed, live), needles, limit=limit,
                                  case_sensitive=False, budget=live, distinct_files=True)
        except (OSError, ValueError, sqlite3.Error):
            budget.omissions.add("Recent source changes could not be checked.")
            return []
        budget.bytes_read = live.bytes_read
        budget.omissions.update(live.omissions)
        return result

    def clear(self, scope: SearchScope) -> int:
        with _guard:
            worker = _workers.get(scope.cache_dir)
            if worker:
                worker[0].set()
        state = _state(scope)
        with state.condition:
            while state.running:
                state.condition.wait(0.08)
            database = _cache_file(scope)
            if not database.is_file():
                return 0
            with closing(_read_db(scope)) as db:
                _meta(db, scope)
                count = db.execute("SELECT count(*) FROM files").fetchone()[0]
            _clear_cache_files(database)
            state.last = None
            state.stale.clear()
            return count


def _queue_refresh(scope: SearchScope, *, force: bool = False):
    if not force:
        try:
            with closing(_read_db(scope)) as db:
                meta = _meta(db, scope)
            if meta is not None and time.time() - meta[1] < _REFRESH_INTERVAL:
                return
        except (OSError, ValueError, sqlite3.Error):
            pass
    with _guard:
        if scope.cache_dir in _workers or len(_workers) >= 16:
            return
        stop = threading.Event()

        def build():
            acquired = False
            try:
                while not stop.is_set() and not acquired:
                    acquired = _worker_slots.acquire(timeout=0.08)
                if acquired and not stop.is_set():
                    RepoIndex().refresh(scope, cancelled=stop)
            except Cancelled:
                pass
            finally:
                if acquired:
                    _worker_slots.release()
                with _guard:
                    _workers.pop(scope.cache_dir, None)

        thread = threading.Thread(target=build, name="project-search-index", daemon=True)
        _workers[scope.cache_dir] = stop, thread
        thread.start()


def close_search_workers():
    """Stop owned refreshes. A later application instance can queue search again."""
    with _guard:
        owned = list(_workers.values())
        for stop, _ in owned:
            stop.set()
    deadline = time.monotonic() + 2.0
    for _, thread in owned:
        thread.join(max(0, deadline - time.monotonic()))


def _related(scope: SearchScope, paths: list[str], seen: set[str]) -> FileCandidate | None:
    if not paths:
        return None
    try:
        with closing(_read_db(scope)) as db:
            if _meta(db, scope) is None:
                return None
            for path in paths[:4]:
                imports = db.execute("SELECT module, member FROM symbols WHERE path=? AND kind='reference' "
                                     "ORDER BY start_line LIMIT 16", (path,)).fetchall()
                for module, member in imports:
                    if not module:
                        continue
                    normalized = module.replace("\\", "/").strip("'\"")
                    if normalized.startswith("."):
                        base = PurePosixPath(path).parent
                        if normalized.startswith(("./", "../")):
                            normalized = os.path.normpath(str(base / normalized)).replace("\\", "/")
                        else:
                            up = len(normalized) - len(normalized.lstrip("."))
                            for _ in range(up - 1):
                                base = base.parent
                            normalized = ".".join((*base.parts, normalized.lstrip("."))).rstrip(".")
                    normalized = normalized.removesuffix(".js").removesuffix(".ts").replace("/", ".").casefold()
                    if not normalized:
                        continue
                    if member and member != "*":
                        rows = db.execute("SELECT s.path, f.sha256, s.start_line, s.end_line FROM symbols s "
                                          "JOIN files f ON f.path=s.path WHERE s.kind='definition' AND s.name=? "
                                          "ORDER BY s.path, s.start_line LIMIT 32", (member.casefold(),)).fetchall()
                    else:
                        rows = db.execute("SELECT s.path, f.sha256, s.start_line, s.end_line FROM symbols s "
                                          "JOIN files f ON f.path=s.path WHERE s.kind='definition' AND lower(s.module)=? "
                                          "ORDER BY s.path, s.start_line LIMIT 16", (normalized,)).fetchall()
                    for target, digest, start, end in rows:
                        candidate = _module_path(target).casefold()
                        if target not in seen and (candidate == normalized or candidate.endswith("." + normalized)):
                            return FileCandidate(target, digest, "reference_candidate", start, end)
    except (OSError, ValueError, sqlite3.Error):
        pass
    return None


def _evidence(path: str, source, names: list[str], words: set[str], *, kind: str, start: int = 0) -> SearchHit:
    lines = source.text.splitlines() or [""]
    if start <= 0:
        phrases = [name.casefold() for name in names]

        def relevance(index: int) -> tuple[int, int, int]:
            line = lines[index]
            return (sum(phrase in line.casefold() for phrase in phrases), len(words.intersection(_words(line))), -index)

        start = max(0, max(range(len(lines)), key=relevance) - 1) + 1 if names else 1
    start = min(start, len(lines))
    excerpt = "\n".join(line[:320] for line in lines[start - 1:start + 4])[:1300]
    return SearchHit(path, start, start + excerpt.count("\n"), excerpt, source.sha256, kind)


def _code_identifier(names: list[str]) -> bool:
    if len(names) != 1 or not _IDENTIFIER.fullmatch(names[0]):
        return False
    name = names[0]
    return bool(_CAMEL.search(name) or any(mark in name for mark in "_$.:") or
                (name.isupper() and len(name) > 2) or any(char.isdigit() for char in name))


def search_project(scope: SearchScope, terms: list[str], *, limit: int = 8, cancelled=None,
                   expand_symbols: bool = True) -> SearchResponse:
    names, words = _terms(terms)
    if not 1 <= limit <= 40:
        raise ValueError("Use 1 to 40 search results.")
    budget = SearchBudget(1.0, cancelled)
    budget.check()
    if not _safe_path(scope.root, within=scope.managed_root).is_dir():
        raise ValueError("Project source folder is unavailable.")
    hits, seen = [], set()
    for name in names:
        relative = normalize_path(name)
        if not relative or relative in seen or not (scope.root / relative).is_file():
            continue
        source = read_source(scope.root, relative, budget)
        if source is not None:
            hits.append(_evidence(relative, source, [], set(), kind="path"))
            seen.add(relative)
        if len(hits) >= limit:
            return budget.response(hits)
    if hits and len(hits) == len(names):
        return budget.response(hits)
    if not words:
        return budget.response(hits)

    index = RepoIndex()
    status = index.status(scope)
    exact_identifier = _code_identifier(names)
    if status.state != "ready":
        needles = names if exact_identifier else list(dict.fromkeys([*names, *words]))
        anchors = [word for word in words if len(word) >= 4]

        def related_name(path: str) -> bool:
            return any(len(part) >= 4 and (word.startswith(part) or part.startswith(word))
                       for part in _words(path) for word in anchors)

        live = literal_hits(scope, needles, pattern="*", limit=limit, case_sensitive=False, budget=budget,
                            distinct_files=True, prioritize=related_name)
        hits.extend(hit for hit in live if hit.path not in seen)
        budget.omissions.add("Local index is warming up. Showing live keyword matches.")
        _queue_refresh(scope)
        return budget.response(hits[:limit])

    budget.omissions.update(status.omissions)
    candidates = index.rank(scope, terms, limit=max(limit, 12))
    stale, definitions, literals, lexical = set(), [], [], []
    folded, tokens = [name.casefold() for name in names], set(words)
    for candidate in candidates:
        if candidate.path in seen or not budget.check():
            continue
        source = read_source(scope.root, candidate.path, budget)
        if source is None or source.sha256 != candidate.sha256:
            stale.add(candidate.path)
            continue
        hit = _evidence(candidate.path, source, names, tokens, kind=candidate.match_kind, start=candidate.start_line)
        if candidate.match_kind == "definition":
            definitions.append(hit)
        elif any(name in hit.excerpt.casefold() for name in folded):
            literals.append(replace(hit, match_kind="exact"))
        else:
            lexical.append(hit)
        seen.add(candidate.path)
        if len(hits) + len(definitions) + len(literals) >= limit:
            break
    recent = []
    if (stale or (exact_identifier and not definitions and not literals)) and budget.check():
        recent = index.search_changes(scope, names, limit=limit, budget=budget)
    seen = {hit.path for hit in (*hits, *definitions, *literals)}
    recent = [hit for hit in recent if hit.path not in seen]
    seen.update(hit.path for hit in recent)
    hits = [*hits, *definitions, *literals, *recent]
    # Broader terms use the index's normal lexical ordering. Explicit identifiers
    # only support related candidates once at least one exact match is present.
    lexical = [hit for hit in lexical if hit.path not in seen and (not exact_identifier or definitions or literals)]
    if not exact_identifier:
        hits = [*hits, *lexical][:limit]
    seen = {hit.path for hit in hits}
    if expand_symbols and len(hits) < limit and budget.check():
        valid = [hit.path for hit in hits if hit.match_kind != "path"]
        related = _related(scope, valid, seen)
        if related is not None:
            source = read_source(scope.root, related.path, budget)
            if source is not None and source.sha256 == related.sha256:
                hits.append(_evidence(related.path, source, names, set(words), kind=related.match_kind,
                                      start=related.start_line))
                seen.add(related.path)
            else:
                stale.add(related.path)
    if exact_identifier:
        hits = [*hits, *(hit for hit in lexical if hit.path not in seen)][:limit]
    if stale:
        budget.omissions.add("Some cached matches changed and were excluded.")
        state = _state(scope)
        with state.condition:
            state.stale.update(stale)
    _queue_refresh(scope, force=bool(stale or recent))
    return budget.response(hits)
