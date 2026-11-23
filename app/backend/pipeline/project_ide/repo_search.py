"""Bounded, read-only literal search in one authorized source tree."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
import fnmatch
import hashlib
import os
from pathlib import Path, PureWindowsPath
import stat
import subprocess
import tempfile
import time

from app.backend.pipeline.agentic.exec import toolchain
from app.backend.pipeline.credentials import is_kept, is_secret_dir
from app.backend.pipeline.git_runtime import isolated_command, isolated_env
from app.backend.pipeline.scan.code_rules import mask

from . import workspace
from .chat_context import _EXTRA_EXCLUDED, _identity, _safe_path
from .resources import Cancelled

MAX_FILES = 10_000
MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
_VISIT_LIMIT = 40_000
_EXACT_SECONDS = 3.0
_RG_OUTPUT_BYTES = 4 * 1024 * 1024
_CONFIG_SUFFIXES = {".ini", ".json", ".properties", ".toml", ".xml", ".yaml", ".yml", ".conf", ".cfg"}


@dataclass(frozen=True)
class SearchScope:
    root: Path
    cache_dir: Path
    managed_root: Path | None = None

    def __post_init__(self):
        object.__setattr__(self, "root", Path(os.path.abspath(self.root)))
        object.__setattr__(self, "cache_dir", Path(os.path.abspath(self.cache_dir)))


@dataclass(frozen=True)
class SearchHit:
    path: str
    start_line: int
    end_line: int
    excerpt: str
    sha256: str
    match_kind: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SearchResponse:
    hits: list[SearchHit]
    complete: bool
    omissions: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SourceText:
    text: str
    sha256: str
    signature: str


class SearchBudget:
    def __init__(self, seconds: float, cancelled):
        self.deadline = time.monotonic() + seconds
        self.cancelled = cancelled
        self.omissions: set[str] = set()
        self.bytes_read = 0

    def check(self) -> bool:
        if self.cancelled is not None and self.cancelled.is_set():
            raise Cancelled("Project search was cancelled.")
        if time.monotonic() >= self.deadline:
            self.omissions.add("Search time limit reached.")
            return False
        return True

    def response(self, hits: list[SearchHit]) -> SearchResponse:
        return SearchResponse(hits, not self.omissions, sorted(self.omissions))


def normalize_path(value: str) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 4096:
        return None
    raw = value.strip().strip("`\"'").replace("\\", "/")
    if raw.startswith("./"):
        raw = raw[2:]
    if not raw or raw.startswith("/") or PureWindowsPath(raw).drive or ":" in raw:
        return None
    if any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in raw):
        return None
    parts = raw.split("/")
    if any(part in {"", ".", ".."} or part.endswith((".", " ")) for part in parts):
        return None
    if workspace.excluded(raw) or is_kept(raw) or any(part.casefold() in _EXTRA_EXCLUDED for part in parts):
        return None
    if any(PureWindowsPath(part).is_reserved() for part in parts):
        return None
    return raw


def _path(root: Path, relative: str, seen_dirs: set[Path] | None = None) -> tuple[Path, os.stat_result]:
    if normalize_path(relative) != relative:
        raise ValueError("Path is outside the project or excluded.")
    path = root / relative
    for parent in path.parents:
        if seen_dirs is not None and parent in seen_dirs:
            break
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("Linked directories are excluded.")
        if parent == root:
            break
    if seen_dirs is not None:
        seen_dirs.add(path.parent)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise ValueError("Only regular files without links can be searched.")
    return path, info


def _signature(info: os.stat_result) -> str:
    return ":".join(str(field) for field in (*_identity(info), info.st_ctime_ns))


def source_signature(root: Path, relative: str, seen_dirs: set[Path]) -> tuple[str, int]:
    _, info = _path(root, relative, seen_dirs)
    return _signature(info), info.st_size


def read_source(root: Path, relative: str, budget: SearchBudget) -> SourceText | None:
    """Recheck the opened file and its path; no unchecked process output becomes evidence."""
    if not budget.check():
        return None
    try:
        path, before = _path(root, relative)
        if before.st_size > MAX_FILE_BYTES or budget.bytes_read + before.st_size > MAX_TOTAL_BYTES:
            budget.omissions.add("Source size limit reached.")
            return None
        with path.open("rb") as stream:
            if _identity(os.fstat(stream.fileno())) != _identity(before):
                raise OSError("Source changed while opening.")
            raw = stream.read(MAX_FILE_BYTES + 1)
            if len(raw) != before.st_size or _identity(os.fstat(stream.fileno())) != _identity(before):
                raise OSError("Source changed while reading.")
        _, after = _path(root, relative)
        if _signature(after) != _signature(before):
            raise OSError("Source changed while reading.")
        budget.bytes_read += len(raw)
        if b"\0" in raw:
            return None
        text = raw.decode("utf-8-sig", errors="strict")
        if "PRIVATE KEY-----" in text:
            return None
        return SourceText(mask(text, config=path.suffix.casefold() in _CONFIG_SUFFIXES),
                          hashlib.sha256(raw).hexdigest(), _signature(before))
    except UnicodeError:
        return None
    except ValueError:
        return None
    except OSError:
        budget.omissions.add("Some source files changed or could not be read.")
        return None


def _process(args: list[str], root: Path, budget: SearchBudget, output_limit: int, *, env=None):
    with tempfile.TemporaryFile() as output:
        with subprocess.Popen(args, cwd=root, stdout=output, stderr=subprocess.DEVNULL, env=env,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as process:
            try:
                while process.poll() is None:
                    if not budget.check() or os.fstat(output.fileno()).st_size > output_limit:
                        return None
                    time.sleep(0.01)
                output.seek(0)
                data = output.read(output_limit + 1)
                return (process.returncode, data) if len(data) <= output_limit else None
            finally:
                if process.poll() is None:
                    process.kill()


def _git_inventory(root: Path, budget: SearchBudget) -> list[str] | None:
    # Non-Git projects need no subprocess. Nested folders in a checkout retain
    # its ignored-file behavior.
    if not any((parent / ".git").exists() for parent in (root, *root.parents)):
        return None
    try:
        result = _process(isolated_command("ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", "."),
                          root, budget, _RG_OUTPUT_BYTES, env=isolated_env())
    except OSError:
        return None
    if result is None or result[0] != 0:
        return None
    names = [name for name in result[1].decode("utf-8", errors="surrogateescape").split("\0") if name]
    return names if names or (root / ".git").exists() else None


def _walk_files(root: Path, budget: SearchBudget):
    pending = [(root, "")]
    visited = 0
    while pending and budget.check():
        folder, prefix = pending.pop()
        try:
            with os.scandir(folder) as entries:
                ordered = sorted(entries, key=lambda item: item.name)
        except OSError:
            budget.omissions.add("Some project folders could not be listed.")
            continue
        children = []
        for entry in ordered:
            if not budget.check():
                return
            visited += 1
            name = prefix + entry.name
            try:
                if entry.is_dir(follow_symlinks=False):
                    if (not is_secret_dir(entry.name) and normalize_path(name) == name
                            and not workspace.linked(Path(entry.path))):
                        children.append((Path(entry.path), name + "/"))
                else:
                    yield name, entry
            except OSError:
                budget.omissions.add("Some project paths changed while listing.")
            if visited >= _VISIT_LIMIT:
                budget.omissions.add("Project inventory limit reached.")
                return
        pending.extend(reversed(children))


def inventory(root: Path, budget: SearchBudget, pattern: str = "*", *, managed_root: Path | None = None) -> list[str]:
    """Take a bounded, sorted inventory using the workspace's exclusions."""
    if not budget.check():
        return []
    if not _safe_path(root, within=managed_root).is_dir():
        raise ValueError("Project source folder is unavailable.")
    names = _git_inventory(root, budget)
    if names is None:
        names = [name for name, _ in _walk_files(root, budget)]
    eligible = sorted({name for name in names if normalize_path(name) == name and fnmatch.fnmatchcase(name, pattern)})
    if len(eligible) > MAX_FILES:
        budget.omissions.add("Project file limit reached.")
    return eligible[:MAX_FILES]


def changed_sources(root: Path, indexed: dict[str, str], budget: SearchBudget):
    """Filter on entry metadata, then let read_source verify any matching file."""
    names = _git_inventory(root, budget)
    if names is None:
        entries = _walk_files(root, budget)
    else:
        entries = ((name, root / name) for name in sorted(set(names)) if normalize_path(name) == name)
    for number, (name, entry) in enumerate(entries):
        if not budget.check():
            return
        if number >= MAX_FILES:
            budget.omissions.add("Project file limit reached.")
            return
        previous = indexed.get(name)
        if previous is not None:
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            # Windows directory entries do not supply a stable inode.
            if previous.rsplit(":", 3)[-3:] == [str(info.st_size), str(info.st_mtime_ns), str(info.st_ctime_ns)]:
                continue
        if normalize_path(name) == name:
            yield name


def _ripgrep() -> Path | None:
    return toolchain.find("ripgrep")


def _batches(paths: list[str]):
    batch, chars = [], 0
    for path in paths:
        if batch and chars + len(path) + 3 > 20_000:
            yield batch
            batch, chars = [], 0
        batch.append(path)
        chars += len(path) + 3
    if batch:
        yield batch


def _rg_candidates(executable: Path, root: Path, names: list[str], needles: list[str], sensitive: bool,
                   budget: SearchBudget) -> Iterable[str]:
    flags = [str(executable), "--no-config", "--files-with-matches", "--fixed-strings", "--max-filesize=512K",
             "--case-sensitive" if sensitive else "--ignore-case"]
    for needle in needles:
        flags.extend(["-e", needle])
    seen_dirs = set()
    for batch in _batches(names):
        if not budget.check():
            break
        eligible = []
        for name in batch:
            if not budget.check():
                break
            try:
                _, size = source_signature(root, name, seen_dirs)
                if size <= MAX_FILE_BYTES:
                    eligible.append(name)
                else:
                    budget.omissions.add("Source size limit reached.")
            except (ValueError, OSError):
                continue
        if not eligible:
            continue
        try:
            result = _process([*flags, "--", *eligible], root, budget, _RG_OUTPUT_BYTES)
            if result is None or result[0] not in {0, 1}:
                yield from eligible
                continue
            matches = {name.replace("\\", "/") for name in result[1].decode("utf-8").splitlines()}
            yield from (name for name in eligible if name in matches)
        except (OSError, UnicodeError):
            yield from eligible


def literal_hits(scope: SearchScope, needles: list[str], *, pattern: str, limit: int, case_sensitive: bool,
                 budget: SearchBudget, distinct_files: bool = False,
                 prioritize: Callable[[str], bool] | None = None) -> list[SearchHit]:
    names = inventory(scope.root, budget, pattern, managed_root=scope.managed_root)
    preferred = [name for name in names if prioritize(name)][:12] if prioritize else []
    matches = _match_files(scope, preferred, needles, limit=limit, case_sensitive=case_sensitive,
                           budget=budget, distinct_files=distinct_files)
    if len(matches) >= limit:
        budget.omissions.add("Search result limit reached.")
        return matches
    sampled = set(preferred)
    names = [name for name in names if name not in sampled]
    executable = _ripgrep()
    # Keep the Python fallback for non-ASCII queries, where Unicode folding
    # behavior may differ between Python and rg.
    use_rg = executable is not None and (case_sensitive or all(needle.isascii() for needle in needles))
    candidates = _rg_candidates(executable, scope.root, names, needles, case_sensitive, budget) if use_rg else names
    return matches + _match_files(scope, candidates, needles, limit=limit - len(matches), case_sensitive=case_sensitive,
                                  budget=budget, distinct_files=distinct_files)


def _match_files(scope: SearchScope, names: Iterable[str], needles: list[str], *, limit: int, case_sensitive: bool,
                 budget: SearchBudget, distinct_files: bool) -> list[SearchHit]:
    if not case_sensitive:
        needles = [needle.casefold() for needle in needles]
    hits = []
    for name in names:
        if not budget.check():
            break
        source = read_source(scope.root, name, budget)
        if source is None:
            continue
        for number, line in enumerate(source.text.splitlines(), 1):
            text = line if case_sensitive else line.casefold()
            if any(needle in text for needle in needles):
                if len(hits) >= limit:
                    budget.omissions.add("Search result limit reached.")
                    return hits
                hits.append(SearchHit(name, number, number, line[:400], source.sha256, "exact"))
                if distinct_files:
                    break
    return hits


def search_exact(scope: SearchScope, term: str, *, pattern: str = "*", limit: int = 40, cancelled=None,
                 case_sensitive: bool = True) -> SearchResponse:
    if not isinstance(term, str) or not term.strip() or len(term) > 300 or any(mark in term for mark in "\0\r\n"):
        raise ValueError("Use one literal search term of 1 to 300 characters.")
    if not isinstance(pattern, str) or len(pattern) > 300 or not 1 <= limit <= 40:
        raise ValueError("Use a bounded file pattern and 1 to 40 results.")
    budget = SearchBudget(_EXACT_SECONDS, cancelled)
    return budget.response(literal_hits(scope, [term], pattern=pattern.replace("\\", "/"), limit=limit,
                                        case_sensitive=case_sensitive, budget=budget))
