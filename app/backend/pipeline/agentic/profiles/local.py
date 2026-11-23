"""The profile of the model on this computer: its file read once, its profile kept, and the live test run once for it (``for_model``).

The identity of a model is its file (size and modification time), its chat template, the build of the server that runs it and the version of the live test. Any of them changing is a new
fingerprint, so a profile is never used for something other than what it was made from."""
from __future__ import annotations

import hashlib
import os
import pathlib
from typing import Callable

from app.backend.pipeline import gguf
from app.backend.pipeline.agentic.profiles import derive, probe, store
from app.backend.pipeline.agentic.profiles.model_profile import ModelProfile, SUITE_VERSION, fingerprint

ENABLED = ("native", "schema", "text")  # the protocols a round can use
_header_cache: dict[tuple, dict] = {}


def _read(path: str) -> dict | None:
    """What the model file says about itself (template, architecture, name, trained context), kept in memory per (path, size, time): a header is cheap to read but every run asks."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, st.st_size, int(st.st_mtime))
    if key not in _header_cache:
        try:
            m = gguf.read_header(path)["metadata"]
        except gguf.GgufError:
            return None
        arch = m.get("general.architecture") if isinstance(m.get("general.architecture"), str) else None
        template = m.get("tokenizer.chat_template")
        _header_cache[key] = {"template": template if isinstance(template, str) else None, "architecture": arch, "name": m.get("general.name"),
                              "context": m.get(f"{arch}.context_length") if arch else None, "size": st.st_size, "mtime": int(st.st_mtime)}
    return _header_cache[key]


def known(path: str, llama_build: str = "") -> ModelProfile | None:
    """The profile already made for this file, or None."""
    info = _read(path)
    return store.load(_fp(info, llama_build)) if info else None


def _fp(info: dict, llama_build: str) -> str:
    return fingerprint(size=info["size"], mtime=info["mtime"], template=hashlib.sha256((info["template"] or "").encode("utf-8")).hexdigest(), llama=llama_build, suite=SUITE_VERSION)


def for_model(path: str, client=None, model: str = "", *, llama_build: str = "", run_probe: bool = True, force: bool = False, on_notice: Callable[[str], None] | None = None) -> ModelProfile | None:
    """The profile of the model file at ``path``: the saved one when the fingerprint matches, else one made from the file and, when ``client`` is given, confirmed by the live test and
    saved. ``force`` makes it again even when one is kept (the owner asked for the test). None when the file cannot be read as GGUF (then nothing is known and the caller keeps its default)."""
    info = _read(path)
    if info is None:
        return None
    fp = _fp(info, llama_build)
    saved = None if force else store.load(fp)
    if saved is not None:
        return saved
    profile = derive.derive(f"local/{pathlib.Path(path).name}", info["template"], architecture=info["architecture"], name=info["name"], filename=pathlib.Path(path).name,
                            context=info["context"] if isinstance(info["context"], int) else None, local=True, fingerprint=fp)
    if run_probe and client is not None:
        if on_notice:
            on_notice("Checking how this model takes tools (once, about half a minute)...")
        result = probe.run(client, model)
        profile.probe = result
        profile.protocol = probe.choose(result, ENABLED)
        profile.source = "probed"
        profile.notes.append(probe.describe(result, profile.protocol))
        store.save(profile)
    return profile
