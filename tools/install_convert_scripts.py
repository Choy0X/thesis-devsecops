"""Install the llama.cpp conversion scripts of one pinned tag into ``data/tools/llama.cpp-convert`` (development tool, standard library only).

The scripts turn a Hugging Face model folder or a LoRA adapter into GGUF files that the llama.cpp server can load. They are Python source in the llama.cpp repository, not part of
its release programs. This tool fetches from the tag you name (for example ``b11528``): ``convert_hf_to_gguf.py``,
``convert_lora_to_gguf.py``, the ``conversion`` package they import (at this tag the model classes live in a folder next to the scripts), the ``gguf`` library from the same
tag (``gguf-py/gguf``, which the scripts load from beside themselves; the release on PyPI lags behind the scripts and fails on them) and the requirements files. Every file is
checked against the git blob hash that the GitHub contents API lists for it before it is written, a name that would leave the folder is refused, and ``VERSION.json`` records the
tag and the SHA-256 of each file so the installed copy is always known. Nothing is downloaded unless you run it.

Run from the repository root::

    .venv\\Scripts\\python -m tools.install_convert_scripts --tag b11528
    .venv\\Scripts\\python -m tools.install_convert_scripts --tag b11528 --list      # what would be fetched, nothing is downloaded
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import pathlib
import shutil
import sys
import urllib.request
from typing import Any

REPO = "ggml-org/llama.cpp"
API = f"https://api.github.com/repos/{REPO}/contents"
RAW = f"https://raw.githubusercontent.com/{REPO}"
DEST = pathlib.Path("data/tools/llama.cpp-convert")
SCRIPTS = ("convert_hf_to_gguf.py", "convert_lora_to_gguf.py")
PACKAGE = "conversion"
LIBRARY = "gguf-py/gguf"                                     # the library the scripts import; only its Python files, not the command-line tools in its scripts folder
REQUIREMENTS = ("requirements/requirements-convert_hf_to_gguf.txt", "requirements/requirements-convert_lora_to_gguf.txt", "requirements/requirements-convert_legacy_llama.txt")
LICENCE = "MIT (https://github.com/ggml-org/llama.cpp/blob/master/LICENSE)"
MAX_BYTES = 8 * 1024 * 1024                                  # no single source file is anywhere near this; a bigger answer is not what was asked for


class InstallError(RuntimeError):
    """A file is not what the tag lists, or the tag is not what was asked for; the message says what."""


def blob_sha(data: bytes) -> str:
    """The hash git gives a file (``blob <size>\\0`` and the bytes), which is what the contents API lists."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _get(url: str, opener=urllib.request.urlopen) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "aivf-install-convert-scripts", "Accept": "application/vnd.github+json"})
    with opener(request, timeout=60) as r:
        data = r.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise InstallError(f"{url} is larger than {MAX_BYTES // 1048576} MB; it was not read")
    return data


def listing(tag: str, opener=urllib.request.urlopen) -> list[dict]:
    """The files to fetch, each as ``{path, size, blob, url}``: the two scripts, the requirements files and the files of the ``conversion`` and ``gguf-py/gguf`` folders."""
    out = []
    for path in (*SCRIPTS, *REQUIREMENTS):
        item = json.loads(_get(f"{API}/{path}?ref={tag}", opener))
        out.append(_entry(item, path, tag))
    for folder in (PACKAGE, LIBRARY):
        items = json.loads(_get(f"{API}/{folder}?ref={tag}", opener))
        if not isinstance(items, list) or not items:
            raise InstallError(f"the tag {tag} has no {folder} folder; this tool is for tags where the scripts import it")
        for item in items:
            if item.get("type") == "file":
                out.append(_entry(item, f"{folder}/{item['name']}", tag))
    return out


def _entry(item: dict, path: str, tag: str) -> dict:
    if not isinstance(item, dict) or item.get("type") != "file" or not item.get("sha"):
        raise InstallError(f"{path} is not a file at {tag} ({item.get('message') if isinstance(item, dict) else item})")
    return {"path": path, "size": int(item["size"]), "blob": str(item["sha"]), "url": f"{RAW}/{tag}/{path}"}


def safe(path: str, dest: pathlib.Path) -> pathlib.Path:
    """``dest / path`` if it stays inside ``dest``, else an error (a name with ``..``, a drive or a leading slash is refused)."""
    p = pathlib.PurePosixPath(path)
    if p.is_absolute() or ":" in path or ".." in p.parts or not p.parts:
        raise InstallError(f"{path!r} would leave the folder; nothing was written")
    target = (dest / p).resolve()
    if not target.is_relative_to(dest.resolve()):
        raise InstallError(f"{path!r} would leave the folder; nothing was written")
    return target


def install(tag: str, dest: pathlib.Path = DEST, opener=urllib.request.urlopen, progress=lambda done, total, path: None) -> dict:
    """Fetch, check and write the files of ``tag`` into ``dest`` and write ``VERSION.json``. An earlier install is replaced only after every file has been fetched and checked."""
    dest = pathlib.Path(dest)
    files = listing(tag, opener)
    staging = dest.with_name(dest.name + ".new")
    shutil.rmtree(staging, ignore_errors=True)
    recorded = {}
    try:
        staging.mkdir(parents=True)
        for n, f in enumerate(files, 1):
            progress(n, len(files), f["path"])
            data = _get(f["url"], opener)
            if len(data) != f["size"] or blob_sha(data) != f["blob"]:
                raise InstallError(f"{f['path']} does not match what GitHub lists for {tag} (size {len(data)} of {f['size']}, or its hash differs); nothing was installed")
            target = safe(f["path"], staging)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            recorded[f["path"]] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "blob": f["blob"]}
        record = {"repository": REPO, "tag": tag, "installed_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "licence": LICENCE,
                  "scripts": list(SCRIPTS), "files": recorded}
        (staging / "VERSION.json").write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
        shutil.rmtree(dest, ignore_errors=True)
        staging.rename(dest)
        return record
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def installed(dest: pathlib.Path = DEST) -> dict | None:
    """What ``VERSION.json`` says is installed, or None."""
    try:
        return json.loads((pathlib.Path(dest) / "VERSION.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--tag", required=True, help="the llama.cpp tag to take the scripts from, for example b11528")
    ap.add_argument("--dest", default=str(DEST))
    ap.add_argument("--list", action="store_true", help="list the files that would be fetched; nothing is downloaded")
    args = ap.parse_args(argv)
    if args.list:
        files = listing(args.tag)
        for f in files:
            print(f"{f['size']:>8}  {f['path']}")
        print(f"{len(files)} files, {sum(f['size'] for f in files) // 1024} kB")
        return 0
    record = install(args.tag, pathlib.Path(args.dest), progress=lambda n, total, path: print(f"\r{n}/{total} {path:<60}", end="", flush=True))
    print(f"\ninstalled {record['tag']} into {args.dest}: {len(record['files'])} files")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except InstallError as e:
        print(f"not installed: {e}", file=sys.stderr)
        sys.exit(1)
