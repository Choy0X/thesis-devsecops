"""Installing a model so that it is ready to run.

Ollama runs GGUF files. A model that comes as safetensors (most of them, and both of the models this project downloads by default) has to be converted first, and the conversion needs
llama.cpp's own scripts. This module says what an install would cost before anything starts (``plan``, from the Hub's metadata alone: nothing is downloaded to plan), what finishing the
install of a model that is already downloaded would cost (``finish_plan``), and runs the steps in one job (``run``): fetch the pinned converter scripts when needed, download the model,
convert it and quantise it.

The safetensors copy is never touched or removed (it is the copy that fine-tuning needs); the converted file goes to ``data/models/converted`` as ``gguf_convert`` writes it. Which format a model is
written in follows its role: a chat model gets the quantisation that suits this computer (``fit.recommend_quant``, Q4_K_M when nothing is known); an embedding model gets Q8_0 in one step,
because its vectors depend on the quantisation and k-quants fall back to larger formats at a hidden size such as 896.
"""
from __future__ import annotations

import pathlib
import re

from app.backend.pipeline import fit, gguf_convert, hub_download, hub_search, models_local

# Measured here: a model of 494 million parameters converted and quantised in 53 s, and one of 5 million in 24 s (the real check of Phase 12B.6). Nothing larger was timed, so the estimate
# says what it is built on.
STARTUP_SECONDS = 20                       # starting the converter (importing its libraries) takes this long whatever the size of the model: a model of 5 million parameters took 24 s in all
SECONDS_PER_MILLION_PARAMETERS = 53 / 494
PINNED_CONVERTER_TAG = "b11528"
SCRIPTS_NOTE = f"about 115 small files, under 2 MB, counted for llama.cpp {PINNED_CONVERTER_TAG}"
ROLES = ("chat", "embedding")
EMBED_REASON = "An embedding model keeps 8 bits (Q8_0): its vectors depend on the quantisation, and a smaller one would also fall back to larger formats at some hidden sizes."


class InstallError(RuntimeError):
    """The install cannot be done, or failed; the message is a sentence for the person."""


def _words(n: int | float) -> str:
    return f"{n / 1024 ** 3:.1f} GB" if n >= 1024 ** 3 else f"{n / 1024 ** 2:.0f} MB"


_TEXT_HEAD = re.compile(r"(ForCausalLM|LMHeadModel|ForConditionalGeneration)$")


def _base_encoder(architectures) -> bool:
    """True when the architectures a model declares are only base ones (``BertModel``, ``Qwen2Model``): without a head that writes text such a model can only produce vectors. One listed
    architecture with a text head (``Qwen2ForCausalLM``) settles it the other way, and no architecture, or only ones with other heads (a classifier), says nothing."""
    names = [a for a in (architectures or ()) if isinstance(a, str)]
    return any(a.endswith("Model") for a in names) and not any(_TEXT_HEAD.search(a) for a in names)


def detect_role(repo_id: str, names=(), architectures=()) -> str:
    """``embedding`` when the name says embed, or the repository (its file names) has the sentence-transformers layout (``modules.json`` and a ``1_Pooling`` folder), or the architecture it
    declares is a base encoder with no text head; else ``chat``. The name comes first because some embedders are built on a text model (Qwen3-Embedding declares ``Qwen3ForCausalLM``)."""
    if re.search("embed", repo_id, re.I):
        return "embedding"
    lowered = [n.lower().replace("\\", "/") for n in names]
    if "modules.json" in lowered and any(n.startswith("1_pooling/") for n in lowered):
        return "embedding"
    return "embedding" if _base_encoder(architectures) else "chat"


def choose_format(role: str, params: int, *, shape=None, hidden: int | None = None, device_profile: dict | None = None, wanted: str | None = None) -> dict:
    """The format a model is written in: ``{outtype, quant, label, reason, fit}``. ``quant`` is the second step (None when the script writes the format itself, as it does for Q8_0)."""
    if role not in ROLES:
        raise InstallError(f"The role {role!r} is not one of {', '.join(ROLES)}.")
    if wanted:
        chosen = wanted.upper()
        if chosen not in gguf_convert.QUANT_BITS:
            raise InstallError(f"The quantisation {wanted!r} is not one of {', '.join(gguf_convert.QUANT_BITS)}.")
        reason, how = "The quantisation was chosen.", None
    elif role == "embedding":
        chosen, reason, how = "Q8_0", EMBED_REASON, None
    else:
        by_size = {q: int(gguf_convert.sizes(params, "f16", q, hidden)["most"]) for q in gguf_convert.QUANT_BITS}
        found = fit.recommend_quant(by_size, device_profile, shape)
        chosen, reason, how = found["quant"], found["reason"], found["fit"]
    if chosen == "Q8_0":
        return {"outtype": "q8_0", "quant": None, "label": "Q8_0", "reason": reason, "fit": how}
    return {"outtype": "f16", "quant": chosen, "label": chosen, "reason": reason, "fit": how}


def scripts_state(tools: pathlib.Path | None = None) -> dict:
    """Report whether the converter needs its pinned script bundle or additional local dependencies."""
    st = gguf_convert.status(tools)
    scripts = st["scripts"]
    changed = bool(scripts) and bool(gguf_convert.verify(tools))
    library = (pathlib.Path(tools or gguf_convert.TOOLS) / "gguf-py" / "gguf" / "__init__.py").is_file()
    will_fetch = scripts is None or changed or not library
    blockers = []
    if not st["quantize"]:
        blockers.append("llama-quantize is not installed. Add it to PATH or install Ollama's Windows package.")
    if st["missing_modules"]:
        blockers.append("These Python packages are missing from the app's environment: " + ", ".join(st["missing_modules"]) + ".")
    return {"tag": PINNED_CONVERTER_TAG if will_fetch else scripts["tag"], "installed": scripts is not None, "installed_tag": scripts["tag"] if scripts else None, "will_fetch": will_fetch,
            "blockers": blockers, "note": SCRIPTS_NOTE if will_fetch else None}


def _parameter_words(params: int) -> str:
    """The size of a model in words: "7.6 billion", and "5 million" for a model too small to be a fraction of a billion (a test model)."""
    return f"{params / 1e9:.1f} billion" if params >= 1e8 else f"{params / 1e6:.0f} million"


def _convert_numbers(params: int, fmt: dict, hidden: int | None) -> dict:
    s = gguf_convert.sizes(params, fmt["outtype"], fmt["quant"], hidden)
    proportional = round(params / 1e6 * SECONDS_PER_MILLION_PARAMETERS)
    seconds = max(STARTUP_SECONDS, proportional)
    return {"intermediate_bytes": int(s["first"]) if fmt["quant"] else 0, "final_bytes": int(s["final"]), "final_bytes_max": int(s["most"]), "peak_bytes": int(s["peak"]), "fallback": s["fallback"],
            "seconds": seconds, "seconds_note": f"An estimate: a model of 0.5 billion parameters took 53 s here and this one has {_parameter_words(params)}; nothing larger was timed."
                                                                     + (f" Starting the converter alone takes about {STARTUP_SECONDS} s." if proportional < STARTUP_SECONDS else "")}


def plan(repo_id: str, role: str | None = None, quant: str | None = None, *, device_profile: dict | None = None, api=None, cache: pathlib.Path | None = None, config_reader=None,
         tools: pathlib.Path | None = None) -> dict:
    """What installing ``repo_id`` so that it is ready to run would do and cost, and every reason it cannot be done. ``kind`` is ``gguf`` (the repository holds GGUF files: a download is all
    it takes, and nothing is converted), ``convert`` (safetensors: download, then convert) or ``other`` (neither: it cannot be made ready here). Only metadata is read."""
    hub = hub_download.plan(repo_id, api=api, cache=cache, with_files=True)
    files = hub.get("file_list") or []
    names = [n for n, _ in files]
    download = {"files": hub["files"], "bytes": hub["bytes"], "partial_bytes": hub["partial_bytes"], "free_bytes": hub.get("free_bytes"), "enough": hub["enough"], "gated": hub["gated"], "token_set": hub["token_set"]}
    base = {"repo_id": repo_id, "revision": hub["revision"], "download": download, "problems": [], "notes": []}
    has_safetensors = any(n.endswith(".safetensors") for n in names)
    if hub["quants"] and not has_safetensors:
        return {**base, "kind": "gguf", "role": role if role in ROLES else detect_role(repo_id, names), "ready": True, "convert": None, "scripts": None,
                "notes": ["This repository holds GGUF files: downloading one quantisation is all it takes, and nothing is converted."]}
    if not has_safetensors:
        return {**base, "kind": "other", "role": None, "ready": False, "convert": None, "scripts": None,
                "problems": ["This repository holds neither safetensors nor GGUF weights, so it cannot be made ready to run here (the converter does not read pickled weights)."]}
    try:
        cfg = (config_reader or hub_search._read_config)(repo_id, hub["revision"])
    except Exception:  # ignored: a configuration that cannot be read is reported as a problem below; the plan still says everything else
        cfg = {}
    arch = (cfg.get("architectures") or [None])[0] if cfg else None
    detected = role not in ROLES
    role = detect_role(repo_id, names, (cfg or {}).get("architectures")) if detected else role
    problems: list[str] = []
    notes: list[str] = []
    if hub["quants"]:
        notes.append(f"This repository also holds GGUF files ({', '.join(hub['quants'])}); downloading one of those would be ready to run without converting.")
    if not cfg:
        problems.append("The model's config.json could not be read, which the converter needs.")
    if gguf_convert.auto_map_in(cfg):
        problems.append("The model's configuration names Python code of its own (auto_map), which the converter could run. It is not converted here.")
    params, estimated = hub.get("parameters"), False
    if not params:
        params, estimated = sum(s for n, s in files if n.endswith(".safetensors")) // 2, True               # weights in 16 bits unless the Hub says otherwise
        notes.append("The Hub does not state the number of parameters; the sizes are estimated from the size of the weight files, counted as 16-bit.")
    hidden = cfg.get("hidden_size") if isinstance(cfg.get("hidden_size"), int) else None
    scripts = scripts_state(tools)
    known = gguf_convert.architectures(tools) if scripts["installed"] else set()
    supported = (arch in known) if (arch and known) else None
    if supported is False:
        problems.append(f"The converter does not list the architecture {arch}.")
    elif supported is None:
        notes.append("Whether the converter knows this architecture is checked once its scripts are installed, before the model is downloaded.")
    fmt = choose_format(role, params, shape=fit.shape_from_config(cfg, params) if cfg else None, hidden=hidden, device_profile=device_profile, wanted=quant)
    numbers = _convert_numbers(params, fmt, hidden)
    if fmt["quant"] and (long_path := gguf_convert.quantize_path_problem(gguf_convert.CONVERTED, gguf_convert.stem({"id": repo_id, "main_revision": hub["revision"]}, fmt["outtype"], fmt["quant"]), fmt["outtype"])):
        problems.append(long_path)                                                                       # found before anything is downloaded, not after the first file was written
    needed = hub["bytes"] + numbers["peak_bytes"]
    free = hub.get("free_bytes")
    if free is not None and needed * (1 + gguf_convert.SPARE) > free:
        problems.append(f"The install needs about {_words(needed)} free (the download and the conversion together, with a tenth to spare) and {_words(free)} is.")
    if hub["gated"] and not hub["token_set"]:
        problems.append("The model is gated and no Hugging Face token is set: accept its terms on its page, make a token there and put it in Settings.")
    problems += scripts["blockers"]
    return {**base, "kind": "convert", "role": role, "role_detected": detected, "outtype": fmt["outtype"], "quant": fmt["quant"], "format": fmt["label"], "format_reason": fmt["reason"],
            "format_fit": fmt["fit"], "parameters": params, "parameters_estimated": estimated, "architecture": arch, "supported": supported, "convert": numbers, "scripts": scripts,
            "disk_needed_bytes": int(needed), "problems": problems, "ready": not problems, "notes": notes}


def _folder_role(entry: dict, folder: pathlib.Path) -> str:
    names = ["modules.json"] if (folder / "modules.json").is_file() else []
    names += ["1_Pooling/config.json"] if (folder / "1_Pooling").is_dir() else []
    return detect_role(entry["id"], names, gguf_convert._config(folder).get("architectures"))


# problems of gguf_convert.plan that ``scripts_state`` covers: an install removes them by itself (it fetches the scripts) or reports them once as blockers, so they are not said twice
_COVERED_BY_SCRIPTS = ("The conversion scripts are not installed", "The scripts are from llama.cpp", "The conversion scripts are not as they were installed", "The gguf library of the scripts is missing",
                       "llama-quantize is not installed", "These Python packages are missing")


def finish_plan(entry: dict, role: str | None = None, quant: str | None = None, *, device_profile: dict | None = None, tools: pathlib.Path | None = None) -> dict:
    """What finishing the install of a model that is already on this computer (an entry of the Model Hub's inventory) would do and cost: the same as ``plan`` without the download, read from
    the model's own folder."""
    if entry.get("source") == "converted" or entry.get("gguf"):
        raise InstallError(f"{entry['id']} holds GGUF files already: it is ready to run.")
    if not entry.get("safetensors"):
        raise InstallError(f"{entry['id']} holds no safetensors weights, so it cannot be converted here.")
    try:
        folder = gguf_convert.model_folder(entry)
    except gguf_convert.ConvertError as e:
        raise InstallError(str(e)) from e
    detected = role not in ROLES
    role = _folder_role(entry, folder) if detected else role
    shape, notes = models_local.read_shape(entry)
    weights, _ = gguf_convert.weight_files(folder)
    params = shape.params or gguf_convert._parameters(weights)
    hidden = shape.hidden if isinstance(shape.hidden, int) else None
    fmt = choose_format(role, params, shape=shape if shape.n_layers else None, hidden=hidden, device_profile=device_profile, wanted=quant)
    scripts = scripts_state(tools)
    cp = gguf_convert.plan(entry, fmt["outtype"], fmt["quant"], tools=tools)
    problems = [p for p in cp["problems"] if not p.startswith(_COVERED_BY_SCRIPTS)] + scripts["blockers"]
    numbers = _convert_numbers(params, fmt, hidden)
    return {"kind": "finish", "repo_id": entry["id"], "source": entry.get("source"), "revision": cp.get("revision"), "role": role, "role_detected": detected, "outtype": fmt["outtype"], "quant": fmt["quant"],
            "format": fmt["label"], "format_reason": fmt["reason"], "format_fit": fmt["fit"], "parameters": params, "parameters_estimated": False, "architecture": cp["architecture"],
            "supported": cp["supported"], "download": None, "convert": numbers, "scripts": scripts, "disk_needed_bytes": numbers["peak_bytes"], "problems": list(dict.fromkeys(problems)),
            "ready": not problems, "notes": list(notes)}


# -- the job ---------------------------------------------------------------------------------------------------------------------------------------

_PERCENT = re.compile(r"(\d{1,3})%\|")


def run(params: dict, *, progress, say, device_profile: dict | None = None, tools: pathlib.Path | None = None, download=None, fetch_scripts=None, convert=None, find=None,
        models_dir: str | None = None, api=None, config_reader=None, adapters=None, room=None) -> dict:
    """The steps of the ``install_model`` job, in this order: the converter scripts (when ``scripts_state`` says they must be fetched: a download from github.com, small), the model (not
    with ``skip_download``: a model that is already on this computer), and the conversion. The plan is made again first and every reason it cannot be done stops the job before anything is
    fetched; the converter's list of architectures is checked as soon as its scripts are in place, so a model it does not know is refused before the download, not after. Each step
    reports ``step`` and ``steps`` and its own numbers; ``say`` is given the lines for the terminal. The safetensors copy is never touched; a failed conversion leaves nothing half-made
    (``gguf_convert.convert`` removes what it started) and the downloaded model stays, so Finish install can be pressed again. Returns the conversion's record."""
    if download is None:
        download = hub_download.download
    if convert is None:
        convert = gguf_convert.convert
    if find is None:
        find = gguf_convert.find
    if adapters is None:
        adapters = gguf_convert.convert_pending_adapters
    if fetch_scripts is None:
        from tools import install_convert_scripts
        fetch_scripts = install_convert_scripts.install
    repo_id = str(params.get("repo_id") or "").strip()
    source = str(params.get("source") or "hub_cache")
    skip = bool(params.get("skip_download"))
    role = params.get("role") or None
    wanted = params.get("quant") or None
    tools = pathlib.Path(tools or gguf_convert.TOOLS)
    if models_dir is None:
        from app.backend.pipeline import model_files
        models_dir = str(model_files.models_dir())
    if skip:
        entry = find(repo_id, source, models_dir)
        if entry is None:
            raise InstallError(f"{repo_id} is not installed on this computer, or it holds GGUF files already.")
        p = finish_plan(entry, role, wanted, device_profile=device_profile, tools=tools)
    else:
        if not hub_download.REPO.match(repo_id):
            raise InstallError("A model is named org/name.")
        p = plan(repo_id, role, wanted, device_profile=device_profile, api=api, config_reader=config_reader, tools=tools)
        if p["kind"] == "gguf":
            raise InstallError("This repository holds GGUF files: download one quantisation, there is nothing to convert.")
    if not p["ready"]:
        raise InstallError(" ".join(p["problems"]))
    names = (["scripts"] if p["scripts"]["will_fetch"] else []) + ([] if skip else ["download"]) + ["convert"]
    total = len(names)

    def step(name: str) -> dict:
        return {"step": names.index(name) + 1, "steps": total, "phase": name}

    say(f"Install    {repo_id}, to run as a {'language' if p['role'] == 'chat' else 'embedding'} model")
    say(f"Format     {p['format']}: {p['format_reason']}")
    say(f"Steps      {', '.join(names)}")
    if "scripts" in names:
        tag = p["scripts"]["tag"]
        fetching = f"Fetching the converter scripts of llama.cpp {tag}"
        progress(state="scripts", label=fetching, processed=0, total=1, unit="files", **step("scripts"))
        say(f"Scripts    llama.cpp {tag} from github.com into {tools}")
        record = fetch_scripts(tag, tools, progress=lambda n, count, path: progress(state="scripts", label=fetching, processed=n, total=count, unit="files", **step("scripts")))
        say(f"Scripts    {len(record.get('files') or {})} files fetched and checked against what GitHub lists")
        if scripts_state(tools)["will_fetch"]:
            raise InstallError("The converter scripts were fetched but are still not usable here; nothing was downloaded.")
        arch, known = p.get("architecture"), gguf_convert.architectures(tools)
        if arch and known and arch not in known:
            raise InstallError(f"The converter does not list the architecture {arch}, so {repo_id} cannot be converted; nothing was downloaded.")
    if "download" in names:
        say("")
        out = download(repo_id, None, progress=lambda **s: progress(**{**s, **step("download")}), say=say)
        entry = find(repo_id, "hub_cache", models_dir)
        if entry is None:
            raise InstallError(f"{repo_id} was downloaded but is not listed among the installed models.")
        say(f"Downloaded {hub_download.size_text(out['bytes'])} in {out['files']} file{'' if out['files'] == 1 else 's'}")
    shown = {"label": f"Converting {repo_id} to {p['format']}"}
    progress(state="converting", label=shown["label"], processed=0, total=100, unit="percent", **step("convert"))

    def log(line: str) -> None:
        say(line)
        if line.startswith("quantising"):
            shown["label"] = f"Quantising {repo_id} to {p['format']}"
        bar = _PERCENT.search(line)
        progress(state="converting", label=shown["label"], processed=int(bar.group(1)) if bar else 0, total=100, unit="percent", **step("convert"))

    record = convert(entry, p["outtype"], p["quant"], tools=tools, log=log, role=p["role"], **({"room": room} if room is not None else {}))
    made = next((f for f in record["files"] if p["quant"] and f["name"].endswith(f"{p['quant']}.gguf")), record["files"][-1])
    say(f"Ready      {made['name']} ({hub_download.size_text(made['bytes'])}, {record['quantisation']}); Settings lists it under {repo_id}")
    try:                                                  # an adapter was waiting for this model, or for the converter scripts that are in place now: whatever can be converted, is
        for line in gguf_convert.adapter_words(adapters(models_dir, tools=tools, log=say)):
            say(line)
    except Exception as e:  # ignored: the model is installed and ready; an adapter that could not be converted now is said so and can be converted later
        say(f"Adapter    was not converted now: {type(e).__name__}: {str(e)[:200]}")
    progress(state="done", label=f"{repo_id} is ready to run", processed=100, total=100, unit="percent", **step("convert"))
    return record
