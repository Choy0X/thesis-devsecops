"""The app-managed command tools for terminal output, search, and Git.

``toolchain.lock.json`` pins one release of each with the SHA-256 GitHub lists for every file (written by ``tools/lock_toolchain.py``, never typed in).
App setup prepares each pinned tool. A download is checked against the pinned size and SHA-256 before anything is unpacked,
the required files are unpacked, and the copy is tested by running it before it counts as installed.
A copy that is installed but no longer passes its test is reported as broken, not used.

The app uses its own copies under ``data/tools/bin`` and never the ``rtk`` that may be on the owner's PATH: the version is the one that was tested, and ``rtk`` runs with its own
configuration and data folders (see ``exec/rtk.py``), so the owner's own settings and history for it are neither read nor changed.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import pathlib
import platform
import shutil
import stat
import subprocess
import sysconfig
import tarfile
import tempfile
import threading
import time
import urllib.request
import zipfile

LOCK = pathlib.Path(__file__).with_name("toolchain.lock.json")
ROOT = pathlib.Path(os.environ.get("ZTSA_TOOLCHAIN_DIR") or "data/tools/bin")        # the variable is for a scratch run (a bench, a test) that works in a folder of its own
CHUNK = 1 << 20
TEST_SECONDS = 30
_install_lock = threading.RLock()
_default_preparation: dict[str, str | None] = {}
_preparation_worker: threading.Thread | None = None


class ToolchainError(RuntimeError):
    """A tool cannot be installed or used; the message says why in words for the owner."""


# -- what is pinned ------------------------------------------------------------------------------------------------------------------------------------------

def load_lock() -> dict:
    return json.loads(LOCK.read_text(encoding="utf-8"))


def platform_key(system: str | None = None, machine: str | None = None) -> str:
    """``windows-x86_64``, ``linux-aarch64``, ``macos-aarch64``...: the platform as the lock file names it."""
    system, machine = (system or platform.system()).lower(), (machine or platform.machine()).lower()
    if system == "windows" and not machine:
        # Python 3.11 reads Windows machine names from PROCESSOR_* variables,
        # which a restricted runtime may omit. Its build still names an architecture it can run.
        machine = {"win-amd64": "amd64", "win-arm64": "arm64"}.get(sysconfig.get_platform(), "")
    os_name = {"windows": "windows", "linux": "linux", "darwin": "macos"}.get(system)
    arch = {"amd64": "x86_64", "x86_64": "x86_64", "x64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}.get(machine)
    if not os_name or not arch:
        raise ToolchainError(f"This computer ({platform.system()} {platform.machine()}) is not one the command tools are built for.")
    return f"{os_name}-{arch}"


def asset_for(tool: str, platform_name: str | None = None) -> dict:
    """The pinned file of ``tool`` for a platform: ``{name, size, sha256, url}``."""
    lock = load_lock()["tools"]
    if tool not in lock:
        raise ToolchainError(f"There is no tool called {tool}.")
    key = platform_name or platform_key()
    asset = lock[tool]["assets"].get(key)
    if asset is None:
        raise ToolchainError(f"{tool} {lock[tool]['version']} has no build for {key}.")
    return asset


def program_name(tool: str, platform_name: str | None = None) -> str:
    base = load_lock()["tools"][tool]["program"]
    return base + (".exe" if (platform_name or platform_key()).startswith("windows") else "")


def program_path(tool: str, platform_name: str | None = None) -> str:
    name = program_name(tool, platform_name)
    return f"cmd/{name}" if tool == "git" else name


# -- what is installed -----------------------------------------------------------------------------------------------------------------------------------------

def folder(tool: str, version: str | None = None, root: pathlib.Path | None = None) -> pathlib.Path:
    version = version or load_lock()["tools"][tool]["version"]
    return pathlib.Path(root or ROOT) / f"{tool}-{version}"


def installed(tool: str, root: pathlib.Path | None = None) -> dict | None:
    """The record of the pinned version of ``tool`` if its files are there and are the ones that were tested, else None."""
    try:
        record = json.loads((folder(tool, root=root) / "INSTALLED.json").read_text(encoding="utf-8"))
        if record["program"] != program_path(tool, record["platform"]):
            return None
        exe = folder(tool, root=root) / record["program"]
        if not exe.is_file() or exe.stat().st_size != record.get("program_size"):
            return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if record.get("version") != load_lock()["tools"][tool]["version"]:
        return None
    return record


def find(tool: str, root: pathlib.Path | None = None) -> pathlib.Path | None:
    """The tested copy of ``tool`` the app runs, or None (nothing on the PATH is ever used in its place)."""
    record = installed(tool, root)
    if record is None or not record.get("tested"):
        return None
    return (folder(tool, root=root) / record["program"]).resolve()               # absolute: a program is started from a project's folder, where a relative path leads nowhere


# -- fetching ----------------------------------------------------------------------------------------------------------------------------------------------------

def fetch(asset: dict, into: pathlib.Path, opener=urllib.request.urlopen, progress=lambda done, total: None) -> pathlib.Path:
    """Download one file into ``into`` and check its size and SHA-256 against the pin; a file that does not match is deleted."""
    into.mkdir(parents=True, exist_ok=True)
    path = into / asset["name"]
    h, done = hashlib.sha256(), 0
    request = urllib.request.Request(asset["url"], headers={"User-Agent": "aivf-toolchain"})
    with opener(request, timeout=120) as r, path.open("wb") as out:
        while block := r.read(CHUNK):
            out.write(block)
            h.update(block)
            done += len(block)
            progress(done, asset["size"])
    if done != asset["size"] or h.hexdigest() != asset["sha256"]:
        path.unlink(missing_ok=True)
        raise ToolchainError(f"{asset['name']} is not the file that was pinned (size {done} of {asset['size']}, or its SHA-256 differs). It was deleted and nothing was installed.")
    return path


def extract(archive: pathlib.Path, into: pathlib.Path, program: str) -> list[str]:
    """Take the program (and the licence files beside it) out of ``archive`` into ``into``. Files are written by their own base name only, never by the path the archive gives them,
    so a name that climbs out or is absolute cannot write anywhere else. The program must be there."""
    into.mkdir(parents=True, exist_ok=True)
    wanted, written = {program.lower()}, []

    def keep(member_name: str) -> str | None:
        base = pathlib.PurePosixPath(member_name.replace("\\", "/")).name
        low = base.lower()
        if low in wanted or low.startswith(("license", "licence", "copying", "unlicense")):
            return base
        return None

    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                base = None if info.is_dir() else keep(info.filename)
                if base:
                    (into / base).write_bytes(z.read(info))
                    written.append(base)
    else:
        with tarfile.open(archive) as t:
            for member in t.getmembers():
                base = keep(member.name) if member.isfile() else None
                if base:
                    f = t.extractfile(member)
                    if f is not None:
                        (into / base).write_bytes(f.read())
                        written.append(base)
    if program not in written:
        raise ToolchainError(f"{archive.name} does not hold {program}.")
    exe = into / program
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return sorted(written)


def extract_distribution_zip(archive: pathlib.Path, into: pathlib.Path) -> list[str]:
    """Extract a verified Windows distribution to a fresh folder, preserving its relative layout."""
    written = []
    seen = set()
    with zipfile.ZipFile(archive) as source:
        for entry in source.infolist():
            parts = entry.filename.rstrip("/").split("/")
            relative = pathlib.PureWindowsPath(*parts)
            folded = entry.filename.rstrip("/").casefold()
            if (folded in seen or relative.is_reserved() or stat.S_ISLNK(entry.external_attr >> 16)
                    or any(not part or part.endswith((".", " ")) or any(ch in part for ch in '\\:<>"|?*') for part in parts)):
                raise ToolchainError(f"{archive.name} contains an unsafe path: {entry.filename!r}")
            seen.add(folded)
            dest = into.joinpath(*parts)
            if entry.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with source.open(entry) as stream, dest.open("xb") as output:
                shutil.copyfileobj(stream, output)
            written.append(entry.filename)
    return sorted(written)


# -- running a tool ----------------------------------------------------------------------------------------------------------------------------------------

def run(argv: list[str], *, timeout: float = TEST_SECONDS, cwd: pathlib.Path | str | None = None, env: dict | None = None, stdin: str | None = None) -> subprocess.CompletedProcess:
    """Run a program without a shell and with nothing to read from unless ``stdin`` is given (a program that waits for input must not wait for the app's own)."""
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, cwd=cwd, env=env, input=stdin,
                          stdin=None if stdin is not None else subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def self_test(tool: str, exe: pathlib.Path) -> tuple[bool, list[str]]:
    """Run the copy and check what the app relies on. ``(passed, what was seen)``; a copy that does not pass is not installed."""
    notes: list[str] = []
    want = load_lock()["tools"][tool]["version"]
    try:
        if tool == "ripgrep":
            version = run([str(exe), "--version"])
            if want not in version.stdout:
                return False, [f"rg --version said {version.stdout.strip()[:80]!r}, not {want}"]
            with tempfile.TemporaryDirectory() as d:
                (pathlib.Path(d) / "a.txt").write_text("alpha\nbeta needle\n", encoding="utf-8")
                found = run([str(exe), "--no-config", "--line-number", "needle", d])
            if found.returncode != 0 or "beta needle" not in found.stdout:
                return False, [f"rg did not find a word in a file it was given (exit {found.returncode})"]
            notes.append(f"ripgrep {want}: found a word in a file")
            return True, notes
        if tool == "rtk":
            from app.backend.pipeline.agentic.exec import rtk as rtk_adapter
            return rtk_adapter.self_test(exe, want)
        if tool == "git":
            with tempfile.TemporaryDirectory(prefix="git-self-test-") as d:
                env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
                env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TEMPLATE_DIR="")
                version = run([str(exe), "--version"], cwd=d, env=env)
                if version.returncode or version.stdout.strip() != f"git version {want}":
                    return False, [f"git --version said {version.stdout.strip()[:80]!r}, expected {want}"]
                init = run([str(exe), "init", "-q"], cwd=d, env=env)
                status = run([str(exe), "status", "--porcelain"], cwd=d, env=env)
                if init.returncode or status.returncode:
                    return False, [f"git could not create and read a local repository: {(init.stderr or status.stderr).strip()[:120]}"]
            return True, [f"MinGit {want}: created and read a local repository"]
    except (OSError, subprocess.SubprocessError) as e:
        return False, [f"{type(e).__name__}: {str(e)[:120]}"]
    return False, [f"there is no test for {tool}"]


# -- installing ----------------------------------------------------------------------------------------------------------------------------------------------

def install(tool: str, *, root: pathlib.Path | None = None, opener=urllib.request.urlopen, progress=lambda name, done, total: None, platform_name: str | None = None) -> dict:
    """Fetch, check, unpack and test the pinned release of ``tool``. The old copy is replaced only after the new one has passed its test; a failure leaves what was there as it was."""
    with _install_lock:
        return _install(tool, root=root, opener=opener, progress=progress, platform_name=platform_name)


def _install(tool: str, *, root: pathlib.Path | None, opener, progress, platform_name: str | None) -> dict:
    lock = load_lock()["tools"][tool]
    key = platform_name or platform_key()
    asset = asset_for(tool, key)
    program = program_path(tool, key)
    target = folder(tool, root=root)
    work = pathlib.Path(tempfile.mkdtemp(prefix=f"{tool}-", dir=_ensure(pathlib.Path(root or ROOT))))
    try:
        archive = fetch(asset, work / "download", opener, lambda d, t: progress(asset["name"], d, t))
        new = work / "unpacked"
        files = extract_distribution_zip(archive, new) if tool == "git" else extract(archive, new, program)
        if program not in files:
            raise ToolchainError(f"{archive.name} does not hold {program}.")
        ok, notes = self_test(tool, new / program) if key == platform_key() else (True, ["not run: this build is for another platform"])
        if not ok:
            raise ToolchainError(f"{tool} {lock['version']} was fetched and checked but did not pass its test, so it was not installed: {'; '.join(notes)}")
        record = {"tool": tool, "version": lock["version"], "tag": lock["tag"], "program": program, "program_size": (new / program).stat().st_size, "asset": asset["name"], "sha256": asset["sha256"],
                  "licence": lock["licence"], "files": files, "tested": key == platform_key(), "notes": notes, "platform": key,
                  "installed_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        (new / "INSTALLED.json").write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
        shutil.rmtree(target, ignore_errors=True)
        _move(new, target)
        return record
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _move(src: pathlib.Path, dst: pathlib.Path, tries: int = 12, wait: float = 0.5) -> None:
    """Rename a folder of freshly written programs. On Windows the scanner of the system may still hold a program that was just run for a moment, and the rename is refused; wait for
    it a few seconds, and copy and delete if it still will not move."""
    for attempt in range(tries):
        try:
            src.rename(dst)
            return
        except PermissionError:
            if attempt + 1 < tries:
                time.sleep(wait)
    shutil.copytree(src, dst)
    shutil.rmtree(src, ignore_errors=True)


def _ensure(path: pathlib.Path) -> pathlib.Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def retest(tool: str, root: pathlib.Path | None = None) -> dict:
    """Run the self-test of the installed copy again and keep the result (a copy that has stopped passing is no longer used)."""
    with _install_lock:
        record = installed(tool, root)
        if record is None:
            raise ToolchainError(f"{tool} is not installed.")
        ok, notes = self_test(tool, folder(tool, root=root) / record["program"])
        record.update({"tested": ok, "notes": notes})
        (folder(tool, root=root) / "INSTALLED.json").write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
        return record


def prepare_defaults() -> None:
    """Install supported missing tools for app setup, keeping any tested copies."""
    for row in status()["tools"]:
        if not row["supported"]:
            continue
        name = row["tool"]
        try:
            with _install_lock:
                if not find(name):
                    _default_preparation[name] = None
                    install(name)
        except (OSError, ToolchainError) as exc:
            _default_preparation[name] = str(exc)
        else:
            _default_preparation.pop(name, None)


def start_defaults() -> None:
    """Let an existing checkout prepare app-managed tools without delaying startup."""
    global _preparation_worker
    if _preparation_worker and _preparation_worker.is_alive():
        return
    for row in status()["tools"]:
        if row["supported"] and not row["tested"]:
            _default_preparation[row["tool"]] = None
    if not _default_preparation:
        return
    _preparation_worker = threading.Thread(target=prepare_defaults, name="command-tools-setup", daemon=True)
    _preparation_worker.start()


def status(root: pathlib.Path | None = None) -> dict:
    """What the owner sees: for each tool, what is pinned, whether this computer has a build for it, and whether a tested copy is installed."""
    out = {"platform": None, "tools": []}
    try:
        key = platform_key()
        out["platform"] = key
    except ToolchainError as e:
        key = None
        out["unsupported"] = str(e)
    preparation = _default_preparation.copy() if root is None else {}
    for tool, pin in load_lock()["tools"].items():
        asset = pin["assets"].get(key) if key else None
        record = installed(tool, root) if asset else None
        pending = tool in preparation and not (record and record.get("tested"))
        out["tools"].append({"tool": tool, "wanted": pin["version"], "licence": pin["licence"], "note": pin["note"], "supported": asset is not None, "size": asset["size"] if asset else None,
                             "installed": record is not None, "tested": bool(record and record.get("tested")), "notes": (record or {}).get("notes", []),
                             "installed_at": (record or {}).get("installed_at"), "preparing": pending and preparation[tool] is None,
                             "preparation_error": preparation[tool] if pending else None})
    return out


def stage_for_image(dest: pathlib.Path, platform_name: str, *, opener=urllib.request.urlopen, progress=lambda name, done, total: None) -> dict:
    """Put the programs of every tool for ``platform_name`` (a Linux build, for the container image) into ``dest/bin``, with their licence files in ``dest/licenses/<tool>``. Each file is
    fetched and checked against the pinned size and SHA-256 and unpacked like an install, but nothing is run (it is built for another system): the image build runs it, once, as its own check.
    Returns ``{tool: {"version", "asset", "sha256"}}``."""
    dest = pathlib.Path(dest)
    (dest / "bin").mkdir(parents=True, exist_ok=True)
    out: dict = {}
    for tool, pin in load_lock()["tools"].items():
        if tool == "git":  # Linux images have Git from their base distribution.
            continue
        asset = asset_for(tool, platform_name)
        program = program_name(tool, platform_name)
        work = pathlib.Path(tempfile.mkdtemp(prefix=f"{tool}-image-", dir=_ensure(dest)))
        try:
            archive = fetch(asset, work / "download", opener, lambda d, t, n=asset["name"]: progress(n, d, t))
            files = extract(archive, work / "unpacked", program)
            shutil.copyfile(work / "unpacked" / program, dest / "bin" / program)
            licences = dest / "licenses" / tool
            licences.mkdir(parents=True, exist_ok=True)
            for name in files:
                if name != program:
                    shutil.copyfile(work / "unpacked" / name, licences / name)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        out[tool] = {"version": pin["version"], "asset": asset["name"], "sha256": asset["sha256"]}
    return out
