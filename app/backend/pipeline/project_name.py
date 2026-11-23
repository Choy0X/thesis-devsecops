"""A name for a project, suggested from its folder.

The evidence, in order of how well it names the project as a whole: the name a package manifest in the folder gives (``package.json``, ``pyproject.toml``, ``Cargo.toml``, ``go.mod``,
``pom.xml``, ``composer.json``, the Gradle settings, ``setup.cfg``, ``setup.py``, ``mix.exs``, a ``.csproj`` or ``.gemspec`` file name), then the name of the git repository (the remote in
``.git/config``), then the title of the README, then the folder's own name. A name that says nothing (``app``, ``src``, ``vite-project``, ``backend``) is skipped, and a folder with such a
name takes its parent's name in front (``acme-shop/backend`` becomes "Acme Shop Backend"). The text is made readable (words split on separators and camel case, acronyms kept), kept
within the limit and made different from the names of the projects that already exist.

Only files in the folder itself are read, each at most ``MAX_READ`` bytes, a link is not followed, and nothing is run: ``setup.py`` and ``mix.exs`` are searched as text, never executed.
"""
from __future__ import annotations

import configparser
import itertools
import json
import pathlib
import re
import tomllib
from typing import Iterable

MAX_READ = 256 * 1024
MAX_LINE = 400                 # a line longer than this is cut before a pattern looks at it (README titles, git config)
MAX_ENTRIES = 5000             # entries of the folder looked at for a .csproj or .gemspec file name

# names that say nothing about the project (compared without case, spaces, dashes and underscores)
GENERIC = {"src", "source", "sources", "app", "apps", "application", "backend", "frontend", "server", "client", "web", "www", "html", "public", "api", "service", "services", "core", "lib",
           "libs", "pkg", "package", "main", "master", "code", "codebase", "project", "projects", "repo", "repos", "repository", "workspace", "work", "dev", "development", "test", "tests",
           "testing", "tmp", "temp", "new", "untitled", "unnamed", "default", "starter", "template", "sample", "example", "examples", "demo", "myapp", "myproject", "viteproject", "reactapp",
           "nextapp", "nextjsapp", "vueapp", "webapp", "website", "site", "tool", "tools", "utils", "misc", "stuff", "dist", "build", "out", "bin", "readme", "title", "documents", "desktop",
           "downloads", "users", "home", "git", "github", "gitlab", "sourcecode"}
ACRONYMS = {"api", "cli", "sdk", "ui", "ux", "db", "sql", "ai", "ml", "llm", "rag", "cve", "osv", "jwt", "oauth", "http", "https", "json", "xml", "csv", "pdf", "ci", "cd", "cms", "crm", "erp",
            "iot", "ssh", "dns", "tls", "ssl", "orm", "gui", "php", "html", "css", "id"}
SMALL = {"a", "an", "and", "of", "the", "for", "to", "in", "on", "or", "vs"}
_MARKER = re.compile(r"[^a-z0-9]")


def _letters(text: str) -> str:
    return _MARKER.sub("", text.lower())


def _generic(text: str) -> bool:
    return _letters(text) in GENERIC


def readable(text: str) -> str:
    """``my_shop-app`` and ``myShopApp`` both become "My Shop App"; an acronym or a deliberately mixed-case word (``iPhone``) is kept."""
    t = re.sub(r"[_\-.]+", " ", text)
    t = re.sub(r"(?<=[a-z][a-z])(?=[A-Z])", " ", t)           # myShop -> my Shop (two lower-case letters first, so iPhone stays whole)
    t = re.sub(r"(?<=\d)(?=[A-Z])", " ", t)
    t = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", t)           # HTTPServer -> HTTP Server
    out = []
    for i, word in enumerate(t.split()):
        low = word.lower()
        if low in ACRONYMS:
            out.append(low.upper())
        elif word.isupper() and len(word) > 1:
            out.append(word)
        elif low in SMALL and i > 0:
            out.append(low)
        elif word.islower():
            out.append(word[:1].upper() + word[1:])
        else:
            out.append(word)
    return " ".join(out)


def _read(path: pathlib.Path) -> str | None:
    """The text of a file in the folder, or None: not a plain file, a link, too large to be a manifest, or unreadable."""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_READ:
            return None
        return path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return None


def _json_name(text: str) -> str | None:
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):                                # damaged, or nested so deeply that the parser gives up
        return None
    name = data.get("name") if isinstance(data, dict) else None
    return name if isinstance(name, str) else None


def _package_json(text: str) -> str | None:
    name = _json_name(text)
    if not name:
        return None
    if name.startswith("@") and "/" in name:
        scope, part = name[1:].split("/", 1)
        return f"{scope} {part}" if _generic(part) else part          # "@acme/web" is Acme Web, "@acme/dashboard" is Dashboard
    return name


def _composer_json(text: str) -> str | None:
    name = _json_name(text)
    return name.split("/")[-1] if name else None


def _toml(text: str) -> dict:
    try:
        return tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError, RecursionError):
        return {}


def _pyproject(text: str) -> str | None:
    data = _toml(text)
    name = (data.get("project") or {}).get("name") or ((data.get("tool") or {}).get("poetry") or {}).get("name")
    return name if isinstance(name, str) else None


def _cargo(text: str) -> str | None:
    name = (_toml(text).get("package") or {}).get("name")
    return name if isinstance(name, str) else None


def _go_mod(text: str) -> str | None:
    module = next((line.strip()[6:].split()[0] for line in text.splitlines() if re.match(r"\s*module\s", line[:300]) and line.strip()[6:].strip()), None)         # line by line: no pattern spans lines
    if not module:
        return None
    parts = [p for p in module.strip('"').split("/") if p]
    while len(parts) > 1 and re.fullmatch(r"v\d+", parts[-1]):         # a major-version suffix is not part of the name
        parts.pop()
    return parts[-1] if parts else None


def _without_blocks(text: str, tag: str) -> str:
    """The text without its ``<tag>...</tag>`` blocks, found with plain searches (each block is skipped once, so the work is linear however the file is built). A block that never
    closes is left as it is: nothing after it can be told apart from its content."""
    out, pos = [], 0
    while True:
        start = text.find(f"<{tag}", pos)
        if start < 0:
            break
        after = text[start + len(tag) + 1:start + len(tag) + 2]
        if after and not (after.isspace() or after == ">"):                       # <parentX is another element
            out.append(text[pos:start + 1])
            pos = start + 1
            continue
        end = text.find(f"</{tag}>", start)
        if end < 0:
            break
        out.append(text[pos:start])
        pos = end + len(tag) + 3
    out.append(text[pos:])
    return "".join(out)


def _tag_text(text: str, tag: str) -> str | None:
    start = text.find(f"<{tag}>")
    if start < 0:
        return None
    start += len(tag) + 2
    end = text.find("<", start)
    return text[start:end].strip() if end > start else None


def _pom(text: str) -> str | None:
    for block in ("parent", "dependencies", "dependencyManagement", "build", "profiles", "reporting", "modules", "properties", "licenses", "developers", "scm"):
        text = _without_blocks(text, block)
    for tag in ("name", "artifactId"):
        found = _tag_text(text, tag)
        if found and "${" not in found:
            return found
    return None


def _gradle_settings(text: str) -> str | None:
    m = re.search(r"rootProject\.name\s*=\s*[\"']([^\"']+)[\"']", text)
    return m.group(1) if m else None


def _setup_cfg(text: str) -> str | None:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(text)
        return parser.get("metadata", "name", fallback=None)
    except configparser.Error:
        return None


def _setup_py(text: str) -> str | None:
    m = re.search(r"\bname\s*=\s*[\"']([^\"']+)[\"']", text)               # searched as text: the file is never run
    return m.group(1) if m else None


def _mix(text: str) -> str | None:
    m = re.search(r"\bapp:\s*:([a-z_][a-z0-9_]*)", text)
    return m.group(1) if m else None


NAMED_FILES = (("package.json", _package_json), ("pyproject.toml", _pyproject), ("Cargo.toml", _cargo), ("go.mod", _go_mod), ("pom.xml", _pom), ("composer.json", _composer_json),
               ("settings.gradle", _gradle_settings), ("settings.gradle.kts", _gradle_settings), ("setup.cfg", _setup_cfg), ("setup.py", _setup_py), ("mix.exs", _mix))
STEM_SUFFIXES = (".csproj", ".fsproj", ".vbproj", ".gemspec")


def _clean(raw: str | None) -> str | None:
    """The readable name, or None when it is empty, an unresolved template, or says nothing about the project."""
    if not raw:
        return None
    text = re.sub(r"\s+", " ", str(raw)).strip()
    if len(text) < 2 or "${" in text or "{{" in text or _generic(text):
        return None
    name = readable(text)
    return None if _generic(name) else name


def _manifest_name(root: pathlib.Path) -> tuple[str, str] | None:
    for file, reader in NAMED_FILES:
        text = _read(root / file)
        name = _clean(reader(text)) if text is not None else None
        if name:
            return name, file
    try:
        entries = sorted(p for p in itertools.islice(root.iterdir(), MAX_ENTRIES) if p.suffix.lower() in STEM_SUFFIXES and p.is_file() and not p.is_symlink())
    except OSError:
        entries = []
    for p in entries:
        name = _clean(p.stem)
        if name:
            return name, p.name
    return None


def _repository_name(root: pathlib.Path) -> str | None:
    text = _read(root / ".git" / "config")
    if not text:
        return None
    urls: dict[str, str] = {}
    remote = None
    for line in text.splitlines():
        line = line[:MAX_LINE].strip()
        m = re.match(r'\[remote\s+"([^"]+)"\]', line)
        if m:
            remote = m.group(1)
        elif line.startswith("["):
            remote = None
        elif remote and (u := re.match(r"url\s*=\s*(.+)", line)):
            urls.setdefault(remote, u.group(1).strip())
    url = urls.get("origin") or next(iter(urls.values()), None)
    if not url:
        return None
    last = [p for p in re.split(r"[/\\:]", url.rstrip("/\\")) if p]
    name = last[-1] if last else ""
    return name[:-4] if name.endswith(".git") else name


def _readme_title(root: pathlib.Path) -> str | None:
    for file in ("README.md", "readme.md", "README.markdown", "README.rst", "README.txt", "README"):
        text = _read(root / file)
        if text is None:
            continue
        for line in text.splitlines()[:40]:
            line = line[:MAX_LINE]                                              # a heading is short; a longer line is cut before any pattern sees it
            m = re.match(r"#\s+(\S.*)", line)
            if not m:
                continue
            title = re.sub(r"<[^>]*>", "", m.group(1).rstrip("# \t"))
            title = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", title)
            title = re.sub(r"[`*_]", "", title)
            title = re.split("\\s[-|:\u2013\u2014]+\\s|:\\s", title)[0].strip()          # "Acme Webshop - a starter" is Acme Webshop
            return title if 2 <= len(title) <= 40 else None
        return None
    return None


def _fit(name: str, limit: int) -> str:
    if len(name) <= limit:
        return name
    cut = name[:limit]
    return (cut.rsplit(" ", 1)[0] if " " in cut and not name[limit].isspace() else cut).strip()


def _unique(name: str, existing: Iterable[str], limit: int) -> str:
    taken = {re.sub(r"\s+", " ", str(e)).strip().lower() for e in existing}
    base = _fit(name, limit)
    if base.lower() not in taken:
        return base
    n = 2
    while True:
        suffix = f" {n}"
        candidate = _fit(name, limit - len(suffix)) + suffix
        if candidate.lower() not in taken:
            return candidate
        n += 1


def suggest(folder: str | pathlib.Path, existing: Iterable[str] = (), *, max_len: int = 80) -> dict:
    """``{"name", "source"}``: the suggested name and where it came from (a file name, "the git repository name", "README.md", "the folder name" or "the folder name and its parent
    folder"). ``existing`` are the names of the projects that already exist; the suggestion is never one of them."""
    root = pathlib.Path(folder)
    found = _manifest_name(root)
    if found:
        return {"name": _unique(found[0], existing, max_len), "source": found[1]}
    for label, raw in (("the git repository name", _repository_name(root)), ("README.md", _readme_title(root))):
        name = _clean(raw)
        if name:
            return {"name": _unique(name, existing, max_len), "source": label}
    parts = [root.name or str(root)]
    parent = root.parent
    while _generic(parts[0]) and len(parts) < 3 and parent != parent.parent:         # a generic folder takes the names above it until one says something
        parts.insert(0, parent.name)
        if not _generic(parent.name):
            break
        parent = parent.parent
    name = readable(" ".join(parts)) or "Project"
    return {"name": _unique(name, existing, max_len), "source": "the folder name" if len(parts) == 1 else "the folder name and its parent folder"}
