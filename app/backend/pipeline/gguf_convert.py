"""Turn a model folder (safetensors) into a GGUF file the local server can load, and a fine-tuned LoRA adapter into a GGUF adapter.

The work is done by llama.cpp's own conversion scripts and its ``llama-quantize`` program, run as child processes; this module checks that they can run, says what the
conversion will cost before it starts, runs it with the network switched off, reads the result back and writes ``conversion.json`` beside it so the file can be traced.

* The scripts are installed by ``tools/install_convert_scripts.py``. Use llama-quantize from PATH or Ollama's Windows installation.
* Nothing is downloaded: the child processes run with ``HF_HUB_OFFLINE=1``. A model is converted from the folder it is already in and is never changed.
* The scripts are third-party Python and a model folder is data from elsewhere, so the conversion is held to what it needs. A model that carries Python code of its own (``*.py`` files,
  ``auto_map`` in a config: some of the scripts' tokenizer paths run such code) or whose weights are not safetensors (a pickle can run code when it is loaded) is refused. The
  installed scripts are checked against the SHA-256 recorded when they were installed, and an extra Python file in their folder refuses the conversion. The child process is given a
  short list of environment variables (the system's own, the cache folders, the offline switches) and none of the app's keys, tokens or passwords. This is not a sandbox: a
  conversion still runs with the rights of the app, so convert models from sources you trust.
* Disk is checked first. The sizes are estimates from the number of parameters and the usual bits per weight of each format; the real file is measured afterwards.
* The converted model goes to ``data/models/converted/<name>/`` (the Model Hub lists that folder); an adapter goes to ``data/models/adapters/<name>/``, which it does not list,
  because an adapter is not a model the server can be started on. An existing output is never overwritten.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

from app.backend import compute
from app.backend.pipeline import gguf, models_local, util

TOOLS = compute.ROOT / "data" / "tools" / "llama.cpp-convert"
CONVERTED = compute.ROOT / "data" / "models" / "converted"
ADAPTERS = compute.ROOT / "data" / "models" / "adapters"
RUNS = compute.ROOT / "training" / "runs"
WINDOWS = os.name == "nt"
MAX_QUANTIZE_PATH = 259          # llama.cpp's programs open files through the classic Windows interface: a path of 260 characters or more (the end mark counted) cannot be opened by them
SCRIPT, LORA_SCRIPT = "convert_hf_to_gguf.py", "convert_lora_to_gguf.py"
OUTTYPES = ("f16", "bf16", "f32", "q8_0")                    # what the script writes itself
ADAPTER_OUTTYPES = ("f16", "bf16", "f32")
BYTES_PER_WEIGHT = {"f32": 4.0, "f16": 2.0, "bf16": 2.0, "q8_0": 8.5 / 8}
QUANT_BITS = {"Q8_0": 8.50, "Q6_K": 6.56, "Q5_K_M": 5.69, "Q5_K_S": 5.54, "Q4_K_M": 4.89, "Q4_K_S": 4.58, "Q3_K_M": 3.91, "Q2_K": 3.35}      # typical bits per weight of llama.cpp's formats
SPARE = 0.10                                                 # a tenth of the estimate, and the room for the next write, are kept free
MODULES = ("torch", "transformers", "safetensors", "numpy", "yaml", "sentencepiece")       # what the scripts import besides the gguf library, which is installed beside them
TAIL = 30
KEEP_ENV = ("PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "HOME", "LOCALAPPDATA", "APPDATA",
            "PROGRAMDATA", "PROGRAMFILES", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "OS", "HF_HOME", "HF_HUB_CACHE", "XDG_CACHE_HOME", "LANG", "LC_ALL")
CODE_SUFFIXES = (".py", ".pyw", ".pyc", ".pyo", ".pyd", ".pyz", ".so", ".dll", ".exe", ".com", ".scr", ".msi", ".pth", ".bat", ".cmd", ".ps1", ".vbs", ".lnk", ".sh")      # nothing a model folder needs; all of it can run
PICKLE_SUFFIXES = (".bin", ".pt", ".pth", ".ckpt", ".pkl", ".pickle")
DATA_JSON = ("tokenizer.json", "vocab.json", "model.safetensors.index.json", "pytorch_model.bin.index.json")      # read as data, by name; every other JSON file is searched for an auto_map
MAX_JSON = 16 * 1024 * 1024
_PROGRESS = re.compile(r"(\d{1,3})%[|]")


class ConvertError(RuntimeError):
    """The conversion cannot be done, or failed; the message is a sentence for the person."""


def _words(n: int | float) -> str:
    return f"{n / 1024 ** 3:.2f} GB" if n >= 1024 ** 3 else f"{n / 1024 ** 2:.0f} MB"


# -- what is installed ---------------------------------------------------------------------------------------------------------------------------

def _record(folder: pathlib.Path) -> dict | None:
    try:
        return json.loads((pathlib.Path(folder) / "VERSION.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def quantize_command() -> list[str] | None:
    """Find the standalone quantizer before checking Ollama's bundled copy."""
    command = shutil.which("llama-quantize")
    if command:
        return [command]
    from app.backend.pipeline.ollama import server
    ollama = server.executable()
    if ollama:
        bundled = pathlib.Path(ollama).parent / "lib" / "ollama" / "llama-quantize.exe"
        if bundled.is_file():
            return [str(bundled)]
    return None


def verify(tools: pathlib.Path | None = None) -> list[str]:
    """What differs between the installed scripts and the record made when they were installed: files that changed, are missing, or were added (any file that is not in the record: an extra
    Python file, a ``.pth`` file or compiled bytecode can run when a script starts). An empty list means they are as installed."""
    tools = pathlib.Path(tools or TOOLS)
    record = _record(tools)
    if not record or not isinstance(record.get("files"), dict):
        return ["there is no record of what was installed"]
    problems = []
    for rel, meta in sorted(record["files"].items()):
        path = tools / rel
        if not path.is_file():
            problems.append(f"{rel} is missing")
        elif _sha256(path) != (meta or {}).get("sha256"):
            problems.append(f"{rel} was changed")
    known = set(record["files"]) | {"VERSION.json"}
    for path in sorted(tools.rglob("*")):
        if path.is_file() and path.relative_to(tools).as_posix() not in known:      # bytecode in a __pycache__ is loaded without its source being read again, so it counts too
            problems.append(f"{path.relative_to(tools).as_posix()} was added")
    return problems


def status(tools: pathlib.Path | None = None) -> dict:
    """Report installed conversion scripts, the quantizer and Python dependencies."""
    tools = pathlib.Path(tools or TOOLS)
    scripts = _record(tools) if (tools / SCRIPT).is_file() and (tools / LORA_SCRIPT).is_file() else None
    quant = quantize_command()
    missing = [m for m in MODULES if importlib.util.find_spec(m) is None]
    problems = []
    if scripts is None:
        problems.append("The conversion scripts are not installed. Run: python -m tools.install_convert_scripts --tag <tag>")
    if quant is None:
        problems.append("llama-quantize is not installed. Add it to PATH or install Ollama's Windows package.")
    if missing:
        problems.append("These Python packages are missing from the app's environment: " + ", ".join(missing) + ".")
    changed = verify(tools) if scripts else []
    if changed:
        problems.append("The conversion scripts are not as they were installed (" + "; ".join(changed[:4]) + (f"; and {len(changed) - 4} more" if len(changed) > 4 else "") + "). Install them again.")
    library = (tools / "gguf-py" / "gguf" / "__init__.py").is_file()
    if scripts and not library:
        problems.append("The gguf library of the scripts is missing from their folder; install the scripts again.")
    return {"scripts": {"tag": scripts["tag"], "files": len(scripts["files"])} if scripts else None, "quantize": bool(quant), "missing_modules": missing,
            "ready": not problems, "problems": problems, "quants": list(QUANT_BITS), "outtypes": list(OUTTYPES)}


def architectures(tools: pathlib.Path | None = None) -> set[str]:
    """The model architectures the installed converter knows, read from the ``@Model.register("...")`` lines of its ``conversion`` package."""
    found: set[str] = set()
    folder = pathlib.Path(tools or TOOLS) / "conversion"
    for f in folder.glob("*.py") if folder.is_dir() else []:
        text = f.read_text(encoding="utf-8", errors="replace")
        for call in re.finditer(r"\.register\(([^)]*)\)", text):
            found.update(re.findall(r"\"([A-Za-z0-9_.\-]+)\"", call.group(1)))
    return found


# -- what a conversion would cost ----------------------------------------------------------------------------------------------------------------

def model_folder(entry: dict) -> pathlib.Path:
    """The folder with the model's files: the snapshot of a cached model, or the folder itself."""
    root = pathlib.Path(entry["path"])
    if entry.get("source") == "hub_cache":
        rev = entry.get("main_revision") or (entry["revisions"][0] if entry.get("revisions") else None)
        if not rev:
            raise ConvertError(f"{entry['id']} has no downloaded revision to convert.")
        return root / "snapshots" / rev
    return root


class _Suspicious(ValueError):
    """A JSON document that says the same key twice: parsers disagree about which one counts."""


def _pairs(pairs):
    keys = [k for k, _ in pairs]
    for key in ("auto_map", "auto_mapping"):
        if keys.count(key) > 1:
            raise _Suspicious(f"{key} is given more than once")
    return dict(pairs)


def json_problem(path: pathlib.Path) -> str | None:
    """Why a JSON file of a model folder is not plainly data, or None. A file that cannot be read as JSON here cannot be checked, so it counts: another reader (the converter's tokenizer
    code, ``transformers``) may take it differently, and so may a document that gives ``auto_map`` twice or nests too deeply for this check."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_pairs)
        return "auto_map" if _auto_map(data) else None
    except _Suspicious as e:
        return str(e)
    except (OSError, ValueError, RecursionError):
        return "cannot be read as JSON, so it cannot be checked"


def _auto_map(obj) -> bool:
    """Whether a parsed JSON document names a class to load from the folder's own code (``auto_map``, or ``auto_mapping`` with a value), at any depth."""
    if isinstance(obj, dict):
        return any(k in ("auto_map", "auto_mapping") and v for k, v in obj.items()) or any(_auto_map(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_auto_map(v) for v in obj)
    return False


def carries_code(folder: pathlib.Path) -> list[str]:
    """What in a model (or adapter) folder could make a conversion run the folder's own code: Python files and other programs (at any depth), and an ``auto_map`` in any JSON file
    (the converter's tokenizer paths load such classes with ``trust_remote_code``). A JSON file too large to search, other than the tokenizer's own data, counts too."""
    found = []
    for p in sorted(folder.rglob("*")):
        if not p.is_file():
            continue
        rel, suffix = p.relative_to(folder).as_posix(), p.suffix.lower()
        if suffix in CODE_SUFFIXES:
            found.append(rel)
        elif suffix == ".json" and p.name not in DATA_JSON:
            try:
                too_large = p.stat().st_size > MAX_JSON
            except OSError:
                too_large = False
            why = "too large to check" if too_large else json_problem(p)
            if why:
                found.append(f"{rel} ({why})")
    return found[:8]


def weight_files(folder: pathlib.Path) -> tuple[list[pathlib.Path], list[str]]:
    """The weight files the converter will load from a model folder, chosen by the converter's own rule, and what is wrong with them. It loads the files called ``model*.safetensors`` (and
    those named in ``model.safetensors.index.json``); only when there are none and no index does it fall back to ``pytorch_model*.bin``, which are pickles. So safetensors files under
    other names do not count, and a folder is refused when the converter would end up reading a pickle."""
    problems = []
    names = sorted(n for n in os.listdir(folder) if n.startswith("model") and n.endswith(".safetensors"))
    index = folder / "model.safetensors.index.json"
    if index.is_file():
        try:
            weight_map = json.loads(index.read_text(encoding="utf-8")).get("weight_map")
        except (OSError, ValueError, AttributeError):
            weight_map = None
        if not isinstance(weight_map, dict):
            problems.append("model.safetensors.index.json has no readable weight map.")
        else:
            for n in sorted({v for v in weight_map.values()}, key=str):
                if not isinstance(n, str) or n != pathlib.PurePath(n).name or not n.endswith(".safetensors") or not (folder / n).is_file():
                    problems.append(f"The weight map names {n!r}, which is not a safetensors file in the model's folder.")
                else:
                    names.append(n)
    names = sorted(set(names))
    if not names and not problems:
        others = sorted(p.name for p in folder.iterdir() if p.suffix.lower() == ".safetensors")
        pickled = any(p.suffix.lower() in PICKLE_SUFFIXES for p in folder.iterdir())
        problems.append("The model has no safetensors weights the converter would read (files called model*.safetensors)."
                        + (f" Its safetensors files are called {', '.join(others[:3])}, and the converter would then fall back to pickled weights." if others and pickled else "")
                        + (" Pickled weights (.bin, .pt) are not converted here: they can run code when they are loaded." if pickled else ""))
    return [folder / n for n in names if (folder / n).is_file()], problems


def _parameters(files: list[pathlib.Path]) -> int:
    total = 0
    for f in files:
        try:
            total += models_local.safetensors_params(f)
        except (OSError, ValueError):
            continue                                                       # a file that is not readable adds nothing; the plan says when no weights were counted
    return total


def _config(folder: pathlib.Path) -> dict:
    try:
        data = json.loads((folder / "config.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def stem(entry: dict, outtype: str, quant: str | None) -> str:
    """The name of the output: the model, a short form of its revision and the format (``Qwen-Qwen2.5-Coder-0.5B-8123ea2e-Q4_K_M``)."""
    rev = (entry.get("main_revision") or (entry.get("revisions") or [""])[0])[:8]
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", entry["id"]).strip("-.") or "model"
    return "-".join(p for p in (base, rev, (quant or outtype).upper()) if p)


def quantize_path_problem(out_root: pathlib.Path, name: str, outtype: str) -> str | None:
    """On Windows: why llama-quantize could not open the file a conversion writes first (a deep folder and a long model name make a path of more than 259 characters; the script, which is
    Python, writes it, and the quantiser then fails with "failed to open GGUF file"), in a sentence; None when the path is short enough or this is not Windows."""
    if not WINDOWS:
        return None
    first = pathlib.Path(os.path.abspath(out_root)) / name / f"{name}-{outtype.upper()}.gguf"
    if len(str(first)) <= MAX_QUANTIZE_PATH:
        return None
    return (f"llama.cpp's quantiser cannot open a file whose path is longer than {MAX_QUANTIZE_PATH} characters on Windows, and the first file of this conversion would be {len(str(first))} "
            f"characters long ({first}). Keep the app in a folder closer to the root of the drive.")


def find(model_id: str, source: str, models_dir: str | None = None) -> dict | None:
    """An installed model of the Model Hub's inventory by id and source, or None. A model that is a GGUF file already (``converted``) is not converted again."""
    if source == "converted":
        return None
    return next((m for m in models_local.inventory(models_dir, converted=CONVERTED)["models"] if m["id"] == model_id and m["source"] == source), None)


def _cut_leftover(out_dir: pathlib.Path) -> bool:
    """Whether ``out_dir`` is what a conversion that was cut short (the job was stopped, the computer lost power) leaves: a folder with no ``conversion.json``, which is written last, that holds
    nothing but GGUF files. Such a folder is not a result and is safe to remove; a folder with anything else in it is not touched."""
    if not out_dir.is_dir():
        return False
    return all(f.is_file() and f.suffix.lower() == ".gguf" for f in out_dir.rglob("*"))                    # a finished folder also holds conversion.json, so it is never one


def sizes(params: int | float, outtype: str = "f16", quant: str | None = "Q4_K_M", hidden: int | None = None) -> dict:
    """What a conversion writes, in bytes, from the number of parameters alone (estimates from the usual bits per weight of each format): ``first`` (the file the script writes), ``final`` (the
    quantised file; the same as ``first`` without a quantisation), ``fallback`` (a k-quant works on rows of 256, so a model whose hidden size is not a multiple of 256 gets larger formats for
    some tensors), ``most`` (the largest the final file can be) and ``peak`` (the disk the conversion holds at once: both files while it quantises)."""
    outtype = (outtype or "f16").lower()
    quant = quant.upper() if quant else None
    first = params * BYTES_PER_WEIGHT[outtype]
    final = params * QUANT_BITS[quant] / 8 if quant else first
    fallback = bool(quant and quant.startswith(("Q2", "Q3", "Q4", "Q5", "Q6")) and isinstance(hidden, int) and hidden % 256)
    most = max(final, params * QUANT_BITS["Q8_0"] / 8) if fallback else final
    return {"first": first, "final": final, "fallback": fallback, "most": most, "peak": first + (most if quant else 0)}


def auto_map_in(config: dict) -> bool:
    """Whether a model's configuration names Python code of its own (``auto_map``): the converter would be asked to run it, so such a model is not converted here."""
    return _auto_map(config)


def plan(entry: dict, outtype: str = "f16", quant: str | None = "Q4_K_M", *, tools: pathlib.Path | None = None, out_root: pathlib.Path | None = None) -> dict:
    """What converting ``entry`` (a model of the Model Hub's inventory) would do and cost, and why it cannot be done when it cannot. Nothing is written."""
    problems = []
    outtype = (outtype or "f16").lower()
    quant = quant.upper() if quant else None
    if outtype not in OUTTYPES:
        raise ConvertError(f"The format {outtype!r} is not one of {', '.join(OUTTYPES)}.")
    if quant and quant not in QUANT_BITS:
        raise ConvertError(f"The quantisation {quant!r} is not one of {', '.join(QUANT_BITS)}.")
    if quant and outtype == "q8_0":
        raise ConvertError("A model that is already Q8_0 is not quantised again; choose f16, bf16 or f32 as the first step, or no quantisation.")
    tools, out_root = pathlib.Path(tools or TOOLS), pathlib.Path(out_root or CONVERTED)
    s = status(tools)
    problems += s["problems"]
    folder = model_folder(entry)
    if not folder.is_dir():
        raise ConvertError(f"The folder of {entry['id']} is not there ({folder}).")
    weights, weight_problems = weight_files(folder)
    params = _parameters(weights)
    cfg = _config(folder)
    arch = (cfg.get("architectures") or [None])[0]
    known = architectures(tools)
    supported = (arch in known) if (arch and known) else None
    if not cfg:
        problems.append("The model has no config.json, which the converter needs.")
    problems += weight_problems
    code = carries_code(folder)
    if code:
        problems.append(f"The model brings Python code of its own ({', '.join(code)}), which the converter could run. It is not converted here.")
    if supported is False:
        problems.append(f"The converter does not list the architecture {arch}.")
    hidden = cfg.get("hidden_size")
    s = sizes(params, outtype, quant, hidden)
    first, final, fallback, most, peak = s["first"], s["final"], s["fallback"], s["most"], s["peak"]
    out_dir = pathlib.Path(out_root) / stem(entry, outtype, quant)
    if quant and (long_path := quantize_path_problem(out_root, out_dir.name, outtype)):
        problems.append(long_path)
    probe = out_dir.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    free = shutil.disk_usage(probe).free
    need = int(peak * (1 + SPARE))
    if params and free < need:
        problems.append(f"The conversion needs about {_words(need)} free and {_words(free)} is.")
    if out_dir.exists() and not _cut_leftover(out_dir):
        problems.append(f"{out_dir.name} is already there; it is never overwritten. Remove it first if you want it made again.")
    return {"model": entry["id"], "source": entry.get("source"), "revision": folder.name if entry.get("source") == "hub_cache" else None, "folder": str(folder),
            "architecture": arch, "supported": supported, "parameters": params, "outtype": outtype, "quant": quant,
            "intermediate_bytes": int(first) if quant else 0, "final_bytes": int(final), "final_bytes_max": int(most), "fallback": fallback, "peak_bytes": int(peak), "free_bytes": free, "output_dir": str(out_dir),
            "output": str(out_dir / f"{out_dir.name}.gguf"), "problems": problems, "ready": not problems,
            "note": "Sizes are estimates from the number of parameters and the usual bits per weight of each format; the file is measured when it is written."}


# -- running -------------------------------------------------------------------------------------------------------------------------------------

def _env() -> dict:
    """The environment of a conversion's child process: the system's own variables and the cache folders, not the app's keys, tokens, passwords or connection strings."""
    keep = {k.upper() for k in KEEP_ENV}
    env = {k: v for k, v in os.environ.items() if k.upper() in keep}
    env.update({"PYTHONIOENCODING": "utf-8", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
                "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1", "TOKENIZERS_PARALLELISM": "false"})
    return env


def _run(cmd: list[str], what: str, log=print) -> None:
    """Run one child process, pass its output on line by line, and raise ``ConvertError`` with the end of its output when it fails."""
    log(f"$ {' '.join(cmd)}")
    tail: list[str] = []
    step = -1
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", env=_env(), cwd=str(compute.ROOT))
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip().encode("ascii", "replace").decode("ascii")                       # the log is plain text; a progress bar's block characters are not
        if not line:
            continue
        bar = _PROGRESS.search(line)
        if bar:                                                                              # a progress bar redraws itself hundreds of times: one line per tenth of it
            if int(bar.group(1)) // 10 == step:
                continue
            step = int(bar.group(1)) // 10
        log(line)
        tail = (tail + [line])[-TAIL:]
    code = proc.wait()
    if code != 0:
        raise ConvertError(_explain(what, code, tail))


def _explain(what: str, code: int, tail: list[str]) -> str:
    text = "\n".join(tail)
    missing = re.search(r"No module named '([\w.]+)'", text)
    if missing:
        return f"{what} needs the Python package {missing.group(1).split('.')[0]}, which is not installed in the app's environment."
    last = next((t for t in reversed(tail) if t.strip()), "no output")
    return f"{what} failed (exit code {code}): {last[:300]}"


def _sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(1 << 20):
            h.update(block)
    return h.hexdigest()


def convert(entry: dict, outtype: str = "f16", quant: str | None = "Q4_K_M", *, tools: pathlib.Path | None = None, out_root: pathlib.Path | None = None,
            quantize: list[str] | None = None, keep_intermediate: bool = False, log=print, role: str | None = None, room=None) -> dict:
    """Convert a model to GGUF (and quantise it): the plan is checked again, the scripts run, the result is read back, and ``conversion.json`` is written beside it. Returns the record.
    The output folder holds only finished files: a failed run removes what it started. ``room(bytes)`` is called with the most the new files can take before anything is written: it makes room
    in the cache of converted models (``derived_cache``) and raises ``CacheFull`` when it cannot, which is a refusal here, before the long part starts."""
    tools = pathlib.Path(tools or TOOLS)
    p = plan(entry, outtype, quant, tools=tools, out_root=out_root)
    if not p["ready"]:
        raise ConvertError(" ".join(p["problems"]))
    if room is not None:
        from app.backend.pipeline import derived_cache
        try:
            room(int(p["final_bytes_max"]))
        except derived_cache.CacheFull as e:
            raise ConvertError(str(e)) from e
    quantize = quantize or quantize_command()
    out_dir = pathlib.Path(p["output_dir"])
    final = pathlib.Path(p["output"])
    first = out_dir / f"{out_dir.name}-{p['outtype'].upper()}.gguf" if p["quant"] else final
    commands = []
    if _cut_leftover(out_dir):
        shutil.rmtree(out_dir)
        log(f"removed what an earlier conversion that was cut short left in {out_dir.name}")
    out_dir.mkdir(parents=True)
    try:
        log(f"converting {entry['id']} ({p['architecture']}, about {_words(p['final_bytes'])} when finished) to {p['outtype']}")
        cmd = [sys.executable, "-B", "-s", str(pathlib.Path(tools) / SCRIPT), p["folder"], "--outfile", str(first), "--outtype", p["outtype"]]
        commands.append(cmd)
        _run(cmd, "The conversion", log)
        if not first.is_file():
            raise ConvertError("The conversion script ended without writing a file.")
        if p["quant"]:
            log(f"quantising to {p['quant']}")
            cmd = [*quantize, str(first), str(final), p["quant"]]
            commands.append(cmd)
            _run(cmd, "The quantisation", log)
            if not final.is_file():
                raise ConvertError("The quantisation ended without writing a file.")
            if not keep_intermediate:
                first.unlink(missing_ok=True)
        info = gguf.describe(final)                                       # a file the server cannot read is not a result
    except Exception:                                                        # whatever went wrong, the folder holds only finished files
        shutil.rmtree(out_dir, ignore_errors=True)
        raise
    files = [{"name": f.name, "bytes": f.stat().st_size, "sha256": _sha256(f)} for f in sorted(out_dir.glob("*.gguf"))]
    now = status(tools)
    record = {"schema": "gguf-conversion/1", "created_at": util.now_zulu(), "kind": "model", "source": {"id": entry["id"], "origin": entry.get("source"), "revision": p["revision"], "folder": p["folder"]},
              "outtype": p["outtype"], "quant": p["quant"], "architecture": info["architecture"], "quantisation": info["quantisation"], "parameters": info["parameters"],
              "source_parameters": p["parameters"], "estimated_bytes": p["final_bytes"], "scripts_tag": (_record(tools) or {}).get("tag"),
              "server": None, "commands": commands, "files": files}
    if role in ("chat", "embedding"):
        record["role"] = role                                                 # what the person installed it as: the pickers of Settings go by it, not by the name of the file
    (out_dir / "conversion.json").write_text(json.dumps(record, indent=1, default=str) + "\n", encoding="utf-8")
    log(f"wrote {final.name}: {_words(final.stat().st_size)}")
    return record


# -- adapters ------------------------------------------------------------------------------------------------------------------------------------

def _same_folder(declared: str, folder: pathlib.Path) -> bool:
    """Whether ``declared`` (the base model an adapter names) is a folder on this computer and is ``folder``: a name that merely ends like the model's does not count."""
    try:
        return pathlib.Path(declared).is_dir() and pathlib.Path(declared).resolve() == pathlib.Path(folder).resolve()
    except OSError:
        return False


def adapter_plan(adapter_dir: str | pathlib.Path, base_entry: dict, outtype: str = "f16", *, name: str | None = None, tools: pathlib.Path | None = None, out_root: pathlib.Path | None = None) -> dict:
    """What converting a PEFT LoRA adapter for serving on top of ``base_entry`` would do. The adapter must name that base model (its ``adapter_config.json``); nothing is written."""
    outtype = (outtype or "f16").lower()
    tools, out_root = pathlib.Path(tools or TOOLS), pathlib.Path(out_root or ADAPTERS)
    if outtype not in ADAPTER_OUTTYPES:
        raise ConvertError(f"The format {outtype!r} is not one of {', '.join(ADAPTER_OUTTYPES)}.")
    folder = pathlib.Path(adapter_dir)
    problems = list(status(tools)["problems"])
    try:
        cfg = json.loads((folder / "adapter_config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ConvertError(f"{folder} has no readable adapter_config.json; it is not a PEFT adapter folder.")
    if not (folder / "adapter_model.safetensors").is_file():
        problems.append("The adapter folder has no adapter_model.safetensors." + (" A pickled adapter (adapter_model.bin) is not converted here: it can run code when it is loaded." if (folder / "adapter_model.bin").is_file() else ""))
    if cfg.get("use_dora"):
        problems.append("This adapter was trained with DoRA, which llama.cpp's adapter converter does not support, so it cannot be served as a GGUF adapter. Train it with plain LoRA to serve it.")
    code = carries_code(folder)
    if code:
        problems.append(f"The adapter folder holds Python code or an auto_map ({', '.join(code)}), which the converter could run. It is not converted here.")
    declared = str(cfg.get("base_model_name_or_path") or "")
    matches = bool(declared) and (declared == base_entry["id"] or _same_folder(declared, model_folder(base_entry)))
    if not matches:
        problems.append(f"The adapter was trained on {declared or 'an unnamed base model'}, not on {base_entry['id']}.")
    base = model_folder(base_entry)
    if not (base / "config.json").is_file():
        problems.append("The base model has no config.json, which the converter needs.")
    if base.is_dir():                                                      # the adapter converter reads the base model too: its folder is held to the rules of a model's
        problems += [f"The base model: {x}" for x in weight_files(base)[1]]
        base_code = carries_code(base)
        if base_code:
            problems.append(f"The base model brings Python code of its own ({', '.join(base_code)}), which the converter could run. It is not used here.")
    else:
        problems.append("The base model's folder is not there.")
    label = name or (folder.parent.name if folder.name.lower() == "adapter" else folder.name)                # a training run keeps its adapter in a folder called adapter: the run is the name
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-.") or "adapter"
    out_dir = pathlib.Path(out_root) / f"{name}-{outtype.upper()}"
    if out_dir.exists() and not _cut_leftover(out_dir):
        problems.append(f"{out_dir.name} is already there; it is never overwritten.")
    size = sum(f.stat().st_size for f in folder.glob("adapter_model.safetensors") if f.is_file())
    return {"adapter": str(folder), "base": base_entry["id"], "base_folder": str(base), "declared_base": declared, "rank": cfg.get("r"), "alpha": cfg.get("lora_alpha"),
            "targets": sorted(cfg.get("target_modules") or []) if isinstance(cfg.get("target_modules"), list) else cfg.get("target_modules"), "outtype": outtype,
            "adapter_bytes": size, "output_dir": str(out_dir), "output": str(out_dir / f"{out_dir.name}.gguf"), "problems": problems, "ready": not problems}


def convert_adapter(adapter_dir: str | pathlib.Path, base_entry: dict, outtype: str = "f16", *, name: str | None = None, tools: pathlib.Path | None = None, out_root: pathlib.Path | None = None, log=print) -> dict:
    """Convert a LoRA adapter to a GGUF adapter the server loads with ``--lora``. The record keeps the SHA-256 of the adapter's own weights, so a served adapter can be traced to the
    training run's file."""
    tools = pathlib.Path(tools or TOOLS)
    p = adapter_plan(adapter_dir, base_entry, outtype, name=name, tools=tools, out_root=out_root)
    if not p["ready"]:
        raise ConvertError(" ".join(p["problems"]))
    out_dir, out = pathlib.Path(p["output_dir"]), pathlib.Path(p["output"])
    weights = pathlib.Path(adapter_dir) / "adapter_model.safetensors"
    cmd = [sys.executable, "-B", "-s", str(pathlib.Path(tools) / LORA_SCRIPT), str(adapter_dir), "--base", p["base_folder"], "--outfile", str(out), "--outtype", p["outtype"]]
    if _cut_leftover(out_dir):
        shutil.rmtree(out_dir)
        log(f"removed what an earlier conversion that was cut short left in {out_dir.name}")
    out_dir.mkdir(parents=True)
    try:
        log(f"converting the adapter {pathlib.Path(adapter_dir).name} (rank {p['rank']}) on {p['base']}")
        _run(cmd, "The adapter conversion", log)
        if not out.is_file():
            raise ConvertError("The adapter conversion ended without writing a file.")
        info = gguf.describe(out)
    except Exception:                                                        # whatever went wrong, the folder holds only finished files
        shutil.rmtree(out_dir, ignore_errors=True)
        raise
    record = {"schema": "gguf-conversion/1", "created_at": util.now_zulu(), "kind": "adapter", "adapter": {"folder": str(adapter_dir), "weights": weights.name, "sha256": _sha256(weights),
              "rank": p["rank"], "alpha": p["alpha"], "declared_base": p["declared_base"]}, "base": {"id": base_entry["id"], "revision": pathlib.Path(p["base_folder"]).name},
              "outtype": p["outtype"], "architecture": info["architecture"], "scripts_tag": (_record(tools) or {}).get("tag"),
              "commands": [cmd], "files": [{"name": out.name, "bytes": out.stat().st_size, "sha256": _sha256(out)}]}
    (out_dir / "conversion.json").write_text(json.dumps(record, indent=1, default=str) + "\n", encoding="utf-8")
    log(f"wrote {out.name}: {_words(out.stat().st_size)}")
    return record


def adapters(root: pathlib.Path | None = None) -> list[dict]:
    """The adapters converted here, newest first: ``{name, path, bytes, sha256, base, revision, rank, trained_from}``. ``trained_from`` is the SHA-256 of the training run's own weights file."""
    out = []
    for record_file in sorted(pathlib.Path(root or ADAPTERS).glob("*/conversion.json")):
        try:
            rec = json.loads(record_file.read_text(encoding="utf-8"))
            f = rec["files"][0]
            path = record_file.parent / f["name"]
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            continue
        if rec.get("kind") != "adapter" or str(f["name"]) != pathlib.PurePath(str(f["name"])).name or not path.is_file():      # the file is in the record's own folder
            continue
        out.append({"name": record_file.parent.name, "path": str(path), "bytes": path.stat().st_size, "sha256": f.get("sha256"), "base": (rec.get("base") or {}).get("id"),
                    "revision": (rec.get("base") or {}).get("revision"), "rank": (rec.get("adapter") or {}).get("rank"), "trained_from": (rec.get("adapter") or {}).get("sha256"),
                    "created_at": rec.get("created_at")})
    return sorted(out, key=lambda a: a.get("created_at") or "", reverse=True)


def base_of(model_file: str | pathlib.Path) -> dict | None:
    """The model a GGUF file was converted from, when it was converted here (its ``conversion.json`` is beside it): ``{id, revision}``; None for a file that was downloaded."""
    try:
        rec = json.loads((pathlib.Path(model_file).parent / "conversion.json").read_text(encoding="utf-8"))
        if rec.get("kind") == "model" and any(f["name"] == pathlib.Path(model_file).name for f in rec.get("files", [])):
            return {"id": rec["source"]["id"], "revision": rec["source"].get("revision")}
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return None

def converted_source_ids(root=None) -> set[str]:
    """The ids of the models that have been converted here (the ``source.id`` of every model ``conversion.json`` under the converted folder): such a model is ready to run, so it is
    not listed as one that cannot be run. A record that cannot be read is skipped."""
    out: set[str] = set()
    for record_file in sorted(pathlib.Path(root or CONVERTED).glob("*/conversion.json")):
        try:
            rec = json.loads(record_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        source = rec.get("source") if isinstance(rec, dict) and rec.get("kind") == "model" else None
        if isinstance(source, dict) and isinstance(source.get("id"), str):
            out.add(source["id"])
    return out


def convert_pending_adapters(models_dir: str | None = None, *, only_base: str | None = None, outtype: str = "f16", tools: pathlib.Path | None = None, runs_root: pathlib.Path | None = None,
                             adapters_root: pathlib.Path | None = None, log=print) -> dict:
    """Convert the adapters of fine-tuning runs that are not converted yet, as far as everything they need is in place, and say what is waiting for what. An adapter is converted when the
    exact model it was trained on is installed as safetensors, the scripts are installed and every check of ``adapter_plan`` passes; nothing is converted from a base that does not match. One that
    cannot be converted yet is ``waiting`` with the reason in a sentence (for instance the model it needs); one whose conversion failed is ``failed``. ``only_base`` limits this to the adapters of
    one base model (what is done when that model has just been installed). Returns ``{converted: [names], waiting: [{run, name, base, reason}], failed: [{run, name, reason}]}``."""
    tools = pathlib.Path(tools or TOOLS)
    done = {a["name"] for a in adapters(adapters_root)}
    out: dict = {"converted": [], "waiting": [], "failed": []}
    for a in run_adapters(runs_root):
        if only_base and a["base"] != only_base:
            continue
        if f"{a['name']}-{outtype.upper()}" in done:
            continue
        waiting = {"run": a["run"], "name": a["name"], "base": a["base"]}
        base = base_entry(a["base"], models_dir)
        if base is None:
            out["waiting"].append({**waiting, "reason": f"it was trained on {a['base'] or 'a model that is not named'}, which is not installed as safetensors; it is converted by itself once that model is installed"})
            continue
        try:
            plan_ = adapter_plan(a["folder"], base, outtype, name=a["name"], tools=tools, out_root=adapters_root)
            if not plan_["ready"]:
                out["waiting"].append({**waiting, "reason": " ".join(plan_["problems"])})
                continue
            convert_adapter(a["folder"], base, outtype, name=a["name"], tools=tools, out_root=adapters_root, log=log)
            out["converted"].append(a["name"])
        except ConvertError as e:
            out["failed"].append({"run": a["run"], "name": a["name"], "reason": str(e)})
    return out


def adapter_words(result: dict) -> list[str]:
    """The lines a job prints about what ``convert_pending_adapters`` did: one per adapter, saying what happened to it. Nothing for an empty result."""
    lines = [f"Adapter    {n}: converted to a GGUF adapter; Settings can load it on its base model" for n in result.get("converted", [])]
    lines += [f"Adapter    {w['name']}: waiting, {w['reason']}" for w in result.get("waiting", [])]
    lines += [f"Adapter    {f['name']}: could not be converted: {f['reason']}" for f in result.get("failed", [])]
    return lines


def converted_role(folder: str | pathlib.Path) -> str | None:
    """What a converted model was installed as, ``chat`` or ``embedding``, from the ``conversion.json`` beside it: the person's choice at the install (the name of a model does not always say
    what it is). None for a record without one (an older conversion) or without a record."""
    try:
        role = json.loads((pathlib.Path(folder) / "conversion.json").read_text(encoding="utf-8")).get("role")
    except (OSError, ValueError, AttributeError):
        return None
    return role if role in ("chat", "embedding") else None


def converted_label(folder: str | pathlib.Path) -> str | None:
    """What a converted model is, in words, from the ``conversion.json`` beside it: the model it was made from and its format (``Qwen/Qwen2.5-Coder-7B-Instruct, Q4_K_M``). None when there is
    no record or it cannot be read, so the caller keeps the name of the file."""
    try:
        rec = json.loads((pathlib.Path(folder) / "conversion.json").read_text(encoding="utf-8"))
        source = rec["source"]["id"]
        fmt = rec.get("quantisation") or rec.get("quant") or str(rec.get("outtype") or "").upper()
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return f"{source}, {fmt}" if isinstance(source, str) and source and fmt else None


def compatible(model_file: str | pathlib.Path, adapter: dict) -> tuple[bool, str]:
    """Whether ``adapter`` may be served on ``model_file``: refused when both are known and the adapter was trained on another base model, allowed with a note when the file was
    downloaded and its base cannot be checked."""
    base = base_of(model_file)
    if base is None:
        return True, "This model file was not converted here, so it cannot be checked that it is the base model the adapter was trained on."
    if adapter.get("base") and base["id"] != adapter["base"]:
        return False, f"The adapter {adapter['name']} was trained on {adapter['base']}, and this model file was converted from {base['id']}."
    if adapter.get("revision") and base.get("revision") and adapter["revision"] != base["revision"]:
        return False, f"The adapter {adapter['name']} was trained on revision {adapter['revision'][:8]} of {adapter['base']}, and this file was converted from revision {base['revision'][:8]}."
    return True, ""


def run_adapters(root: pathlib.Path | None = None) -> list[dict]:
    """The fine-tuning runs that have an adapter folder, up to four folders down: ``{run, name, folder, base, rank, bytes}``. ``run`` is the path below ``root`` with forward slashes; it is the
    only thing a request names, so a request cannot point the converter at an arbitrary folder."""
    root = pathlib.Path(root or RUNS)
    found = []
    for depth in range(1, 5):
        pattern = "/".join(["*"] * depth) + "/adapter/adapter_config.json"
        for cfg_file in sorted(root.glob(pattern)):
            folder = cfg_file.parent
            weights = folder / "adapter_model.safetensors"
            try:
                cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not weights.is_file() or not isinstance(cfg, dict):
                continue
            run = folder.parent.relative_to(root).as_posix()
            found.append({"run": run, "name": run.replace("/", "-"), "folder": str(folder), "base": cfg.get("base_model_name_or_path"), "rank": cfg.get("r"), "bytes": weights.stat().st_size})
    return found


def base_entry(base_id: str | None, models_dir: str | None = None) -> dict | None:
    """The installed model (stored as safetensors) that a base model id names, or None."""
    if not base_id:
        return None
    return next((m for m in models_local.inventory(models_dir, converted=CONVERTED)["models"] if m["id"] == base_id and m["safetensors"] and m["source"] != "converted"), None)
