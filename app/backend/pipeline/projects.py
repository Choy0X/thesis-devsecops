"""Projects: code the person owns or is authorised to test, added to the app so that it can be asked about, scanned and patched.

A project is a name, one or more source folders on this computer, and an acknowledgement, stored with its time, that the person owns the code or is authorised to
test it. Nothing in the app scans, copies or executes a project without that acknowledgement, and every tool run is appended to the project's audit log. The source
folders are only read, with one exception: applying a dependency update (``scan/update_plan.py``) writes the manifests it names, only after the person has confirmed that list of
files, with a backup and an undo. Deleting a project removes its record and its results, never its folders.

Folders are checked on the server, not trusted from the page: they are made absolute (a symbolic link or a junction is followed to the real folder), they must exist, and
a few places are refused outright (a drive's root, the system and program folders, the folder of the user's profile itself, and the app's own data), because a project there
would make the app read or copy what no project should contain.

Stored as plain files under ``data/projects/<id>/`` (``project.json``, ``audit.jsonl``, later ``scans/``): small, local, inspectable, and written atomically.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import secrets
import shutil
import time

from app.backend.pipeline import fsio, project_name, util

ROOT = pathlib.Path("data/projects")                       # relative to the working folder, like the other run folders
STATEMENT = "I own this code, or I am authorised to test it. The app may read it and copy it into a sandbox to analyse it."
SCHEMA = "project/1"
MAX_FOLDERS = 8
MAX_NAME = 80
MAX_INSTRUCTIONS = 4000
SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__", ".tox", ".idea", ".vscode", "dist", "build", "target", "vendor", ".gradle", ".next", ".cache"}
MAX_FILES = 50_000
DETECT_SECONDS = 6.0
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,60}$")

# manifest file names (or patterns, by suffix) -> the package ecosystem of OSV they belong to
MANIFESTS: dict[str, tuple[str, ...]] = {
    "PyPI": ("requirements.txt", "requirements-dev.txt", "pyproject.toml", "Pipfile", "Pipfile.lock", "poetry.lock", "setup.py", "setup.cfg"),
    "npm": ("package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml"),
    "Maven": ("pom.xml", "build.gradle", "build.gradle.kts", "gradle.lockfile"),
    "Go": ("go.mod", "go.sum"),
    "crates.io": ("Cargo.toml", "Cargo.lock"),
    "RubyGems": ("Gemfile", "Gemfile.lock"),
    "Packagist": ("composer.json", "composer.lock"),
    "NuGet": ("packages.config", "packages.lock.json", "Directory.Packages.props"),
    "Hex": ("mix.exs", "mix.lock"),
}
SUFFIX_MANIFESTS = {".csproj": "NuGet", ".fsproj": "NuGet", ".vbproj": "NuGet", ".gemspec": "RubyGems"}
LANGUAGES = {".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript", ".java": "Java",
             ".kt": "Kotlin", ".go": "Go", ".rs": "Rust", ".rb": "Ruby", ".php": "PHP", ".cs": "C#", ".ex": "Elixir", ".exs": "Elixir"}


class ProjectError(ValueError):
    """What is wrong with a project or a folder, as a sentence for the person."""


class NotFound(ProjectError):
    pass


# -- folders -----------------------------------------------------------------------------------------------------------------------------------------------------

def _refused_places() -> list[pathlib.Path]:
    places = []
    for var in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
        v = os.environ.get(var)
        if v:
            places.append(pathlib.Path(v).resolve())
    return places


def refusal(folder: pathlib.Path) -> str | None:
    """Why this real folder cannot be a source folder, or None."""
    if folder.parent == folder:
        return "A drive's root is too broad to be a project; choose the folder of the project itself."
    if any(folder == p or p in folder.parents for p in _refused_places()):
        return "System and program folders cannot be projects."
    home = pathlib.Path(os.path.expanduser("~")).resolve()
    if folder == home:
        return "Your user folder as a whole is too broad to be a project; choose the folder of the project itself."
    for own in (pathlib.Path("data").resolve(), ROOT.resolve()):
        if folder == own or own in folder.parents:
            return "The app's own data folder cannot be a project."
    return None


def validate_folder(path: str) -> pathlib.Path:
    """The real, absolute folder behind ``path``; raises ``ProjectError`` in words when it is not a usable source folder."""
    text = str(path or "").strip().strip('"')
    if not text or "\x00" in text:
        raise ProjectError("A source folder is needed.")
    p = pathlib.Path(text).expanduser()
    if not p.is_absolute():
        raise ProjectError(f"{text} is not a full path. Give the whole path of the folder, for example C:\\Users\\you\\code\\app.")
    try:
        real = p.resolve(strict=True)                       # follows a link or a junction to the folder it stands for
    except (OSError, RuntimeError):
        raise ProjectError(f"{text} does not exist, or cannot be read.") from None
    if not real.is_dir():
        raise ProjectError(f"{text} is a file; a project is a folder.")
    why = refusal(real)
    if why:
        raise ProjectError(why)
    try:
        next(real.iterdir(), None)
    except OSError:
        raise ProjectError(f"{text} cannot be read with the rights this app has.") from None
    return real


def inside(root: str, path: str) -> bool:
    """Whether ``path`` really is inside ``root`` (a real, absolute folder): links, junctions and ``..`` are resolved first, so a link that leads out of a project is outside. Every
    walk of a project's folders (the code scan, the project index, the chat's tools) asks this before it reads a file or enters a folder."""
    try:
        return os.path.commonpath([root, os.path.realpath(path)]) == root
    except ValueError:
        return False


def detect(folder: str | pathlib.Path, *, max_files: int = MAX_FILES, seconds: float = DETECT_SECONDS) -> dict:
    """What a folder holds: the languages by file count, the package ecosystems by the manifests found, and the manifests. A bounded walk that skips dependency and build
    folders and never opens a file."""
    root = pathlib.Path(folder)
    langs: dict[str, int] = {}
    ecos: dict[str, list[str]] = {}
    seen, truncated, end = 0, False, time.monotonic() + seconds
    for dirpath, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(".") or d in (".github",))
        for name in files:
            seen += 1
            if seen > max_files or time.monotonic() > end:
                truncated = True
                break
            rel = str(pathlib.PurePath(dirpath, name).relative_to(root)).replace("\\", "/")
            ext = pathlib.Path(name).suffix.lower()
            if ext in LANGUAGES:
                langs[LANGUAGES[ext]] = langs.get(LANGUAGES[ext], 0) + 1
            eco = next((e for e, names in MANIFESTS.items() if name in names or (e == "PyPI" and re.fullmatch(r"requirements[-_.\w]*\.txt", name))), None) or SUFFIX_MANIFESTS.get(ext)
            if eco:
                ecos.setdefault(eco, []).append(rel)
        if truncated:
            break
    return {"languages": dict(sorted(langs.items(), key=lambda kv: -kv[1])), "ecosystems": sorted(ecos), "manifests": {e: sorted(v)[:50] for e, v in sorted(ecos.items())},
            "files_seen": min(seen, max_files), "truncated": truncated}


# -- the records ---------------------------------------------------------------------------------------------------------------------------------------------------

def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30].strip("-")
    return s or "project"


def _dir(project_id: str) -> pathlib.Path:
    if not _ID.match(project_id or ""):
        raise NotFound("There is no such project.")
    return ROOT / project_id


def _read(project_id: str) -> dict:
    path = _dir(project_id) / "project.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise NotFound("There is no such project.") from None


def _write(project: dict) -> None:
    d = _dir(project["id"])
    d.mkdir(parents=True, exist_ok=True)
    fsio.write_json(d / "project.json", project, indent=2)


def _folders(paths: list[str]) -> list[dict]:
    if not paths:
        raise ProjectError("At least one source folder is needed.")
    if len(paths) > MAX_FOLDERS:
        raise ProjectError(f"A project has at most {MAX_FOLDERS} source folders.")
    real = [validate_folder(p) for p in paths]
    if len({str(r).lower() for r in real}) != len(real):
        raise ProjectError("The same folder is listed twice.")
    for a in real:
        for b in real:
            if a != b and a in b.parents:
                raise ProjectError(f"{b} is inside {a}; list the outer folder only.")
    return [{"path": str(r), **detect(r)} for r in real]


def _summary(folders: list[dict]) -> dict:
    langs: dict[str, int] = {}
    for f in folders:
        for k, v in f["languages"].items():
            langs[k] = langs.get(k, 0) + v
    return {"languages": dict(sorted(langs.items(), key=lambda kv: -kv[1])), "ecosystems": sorted({e for f in folders for e in f["ecosystems"]})}


def _name(name: str) -> str:
    name = re.sub(r"\s+", " ", str(name or "")).strip()
    if not name:
        raise ProjectError("A project needs a name.")
    if len(name) > MAX_NAME:
        raise ProjectError(f"A project's name has at most {MAX_NAME} characters.")
    return name


def suggest_name(path: str) -> dict:
    """``{"name", "source"}``: a name for the project that this folder would make, never the name of a project that already exists (``project_name.suggest``)."""
    return project_name.suggest(validate_folder(path), [p.get("name", "") for p in list_projects()], max_len=MAX_NAME)


def create(name: str, folders: list[str], *, authorized: bool) -> dict:
    """Add a project. Refused, in words, without the acknowledgement that the person owns the code or may test it. The name may be left empty: it is then suggested from the
    (first) folder, the way the page suggests it."""
    if authorized is not True:
        raise ProjectError("A project can be added only when you confirm that you own the code or are authorised to test it.")
    entries = _folders(list(folders))
    name = _name(name) if str(name or "").strip() else suggest_name(entries[0]["path"])["name"]
    project_id = f"{_slug(name)}-{secrets.token_hex(3)}"
    project = {"schema": SCHEMA, "id": project_id, "name": name, "folders": entries, **_summary(entries), "created_at": util.now_zulu(), "updated_at": util.now_zulu(),
               "authorization": {"acknowledged": True, "at": util.now_zulu(), "statement": STATEMENT}, "last_scan": None, "instructions": ""}
    _write(project)
    audit(project_id, tool="project", summary=f"created with {len(entries)} source folder{'s' if len(entries) != 1 else ''}", target=", ".join(f["path"] for f in entries))
    return project


def get(project_id: str) -> dict:
    return _read(project_id)


def list_projects() -> list[dict]:
    """Every project, newest first. A folder that is not a project (or a damaged record) is skipped."""
    out = []
    for d in sorted(ROOT.iterdir()) if ROOT.is_dir() else []:
        try:
            out.append(_read(d.name))
        except NotFound:
            continue
    return sorted(out, key=lambda p: p.get("created_at", ""), reverse=True)


def update(project_id: str, *, name: str | None = None, folders: list[str] | None = None, instructions: str | None = None) -> dict:
    """Rename a project, change its source folders (checked again, and the languages and ecosystems read again) or its chat instructions. The acknowledgement is kept as it was
    given. ``instructions`` is None when not given (left as it was); an empty string clears a custom override back to the built-in default."""
    p = _read(project_id)
    changes = []
    if name is not None:
        p["name"] = _name(name)
        changes.append("renamed")
    if folders is not None:
        p["folders"] = _folders(list(folders))
        p.update(_summary(p["folders"]))
        changes.append("source folders changed")
    if instructions is not None:
        instructions = instructions.strip()
        if len(instructions) > MAX_INSTRUCTIONS:
            raise ProjectError(f"The chat instructions have at most {MAX_INSTRUCTIONS:,} characters.")
        p["instructions"] = instructions
        changes.append("chat instructions changed" if instructions else "chat instructions reverted to the default")
    p["updated_at"] = util.now_zulu()
    _write(p)
    if changes:
        audit(project_id, tool="project", summary=", ".join(changes), target=", ".join(f["path"] for f in p["folders"]))
    return p


def delete(project_id: str) -> None:
    """Remove the project's record, audit log and results. The source folders are not touched."""
    d = _dir(project_id)
    _read(project_id)
    shutil.rmtree(d)


def record_scan(project_id: str, summary: dict) -> dict:
    """Remember the newest scan on the project (``last_scan``: when, which, and a sentence); the report itself is in the project's ``scans`` folder."""
    p = _read(project_id)
    p["last_scan"] = summary
    p["updated_at"] = util.now_zulu()
    _write(p)
    return p


def scans(project_id: str) -> list[dict]:
    """The reports of the project's scans, newest first, as ``{scan_id, kind, finished, summary}``."""
    _read(project_id)
    out = []
    root = _dir(project_id) / "scans"
    for d in sorted(root.iterdir(), reverse=True) if root.is_dir() else []:
        try:
            r = json.loads((d / "report.json").read_text(encoding="utf-8"))
            out.append({"scan_id": r["scan_id"], "kind": r.get("kind"), "finished": r.get("finished"), "summary": r.get("summary")})
        except (OSError, ValueError, KeyError):
            continue
    return out


def scan_report(project_id: str, scan_id: str) -> dict:
    _read(project_id)
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{4}", scan_id or ""):
        raise NotFound("There is no such scan.")
    try:
        return json.loads((_dir(project_id) / "scans" / scan_id / "report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise NotFound("There is no such scan.") from None


def require_authorized(project: dict) -> None:
    """Called before anything reads the project's code for a scan or copies it: refuses a project that has no acknowledgement."""
    a = project.get("authorization") or {}
    if not (a.get("acknowledged") is True and a.get("at")):
        raise ProjectError("This project has no confirmation that you own the code or may test it, so it cannot be scanned.")


def index_roots(project: dict) -> list[tuple[str, str]]:
    """The folders of a project as its search index reads them: ``(prefix, path)``, the prefix being the folder's name when the project has several (so two files of one name do not meet)."""
    folders = [(os.path.basename(os.path.realpath(f["path"]).rstrip("\\/")) or "project", f["path"]) for f in project["folders"]]
    return [(label if len(folders) > 1 else "", path) for label, path in folders]


# -- the audit log ---------------------------------------------------------------------------------------------------------------------------------------------------

def audit(project_id: str, *, tool: str, summary: str, target: str = "", started: str | None = None, ended: str | None = None, exit_status: int | str | None = None,
          evidence: str | None = None) -> dict:
    """Append one line to the project's audit log: the tool, what it was asked in a sentence (no secrets, no file contents), the target, the times, the exit status and the file
    that holds the evidence. The log is only ever appended to."""
    entry = {"at": util.now_zulu(), "tool": tool, "summary": str(summary)[:300], "target": str(target)[:500], "started": started or util.now_zulu(), "ended": ended, "exit_status": exit_status, "evidence": evidence}
    d = _dir(project_id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "audit.jsonl"
    torn = False                                              # a power cut in the middle of a line leaves it without its end; the next entry must not be glued to it
    if path.exists() and path.stat().st_size:
        with path.open("rb") as f:
            f.seek(-1, os.SEEK_END)
            torn = f.read(1) != b"\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(("\n" if torn else "") + json.dumps(entry, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return entry


def audit_log(project_id: str, limit: int = 500) -> list[dict]:
    """The newest ``limit`` entries, newest first; a damaged line is skipped, never repaired."""
    _read(project_id)
    path = _dir(project_id) / "audit.jsonl"
    out = []
    for line in (path.read_text(encoding="utf-8").splitlines() if path.exists() else []):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return list(reversed(out))[: max(1, int(limit))]


def audit_export(project_id: str) -> str:
    """The whole audit log as lines of JSON, oldest first, for a person to keep or hand over."""
    _read(project_id)
    path = _dir(project_id) / "audit.jsonl"
    return path.read_text(encoding="utf-8") if path.exists() else ""
