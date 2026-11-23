"""Downloading a model from the Hugging Face Hub into the cache the rest of the app already reads (``models_local``).

Three rules keep it safe. The download is pinned to the commit the Hub reports when it is planned, so a model that changes while it downloads cannot mix two
versions. It is refused before it starts when the disk cannot hold it (with room to spare). And a model with only GGUF files needs the quantisation chosen,
because those repositories hold several copies of the same weights and downloading all of them would waste tens of gigabytes. A broken download resumes where
it stopped (the library keeps the partial files); stopping one ends the process and keeps the part already fetched, which "Download" continues from and which can be discarded
(only the unfinished files of that one model) or kept; a gated model is refused in words before anything is fetched when no token is set, and again when the token has no access.
A downloaded model can be removed from the cache (only a folder of the cache, and never while a job or the model server is using it). Nothing here runs unless the person asks.
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil

from app.backend.pipeline import fit, hub_search, models_local

MARGIN = 1.10                       # the disk must hold the model with a tenth to spare
SAFETENSORS_PATTERNS = ["*.safetensors", "*.safetensors.index.json", "*.json", "*.txt", "*.model", "*.tiktoken", "*.jinja"]


def cache_dir() -> pathlib.Path:
    return models_local.hub_cache_dir()


REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")


def token_set() -> bool:
    """Whether a Hugging Face token is set for this process (Settings puts it in HF_TOKEN for the jobs it starts)."""
    return bool((os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN") or "").strip())


def repo_folder(repo_id: str, cache: pathlib.Path | None = None) -> pathlib.Path:
    """The cache folder of a model (``models--org--name``), checked to lie inside the cache: a name that is not ``org/name`` is refused, so nothing else can be reached."""
    if not isinstance(repo_id, str) or not REPO.match(repo_id) or ".." in repo_id:
        raise ValueError("A model is named org/name.")
    root = pathlib.Path(cache or cache_dir())
    folder = root / ("models--" + repo_id.replace("/", "--"))
    if not folder.resolve().is_relative_to(root.resolve()):
        raise ValueError("That is not a model of the cache.")
    return folder


def _size(paths) -> int:
    total = 0
    for f in paths:
        try:
            total += f.stat().st_size
        except OSError:
            continue
    return total


def _tree_bytes(folder: pathlib.Path) -> int:
    """The bytes a model folder takes: every regular file in it once. A symlink (a snapshot entry that points at a blob) is not counted again; on a computer where the
    cache cannot use symlinks the files are in the snapshots and the blobs folder is empty, so both are read."""
    total = 0
    for f in folder.rglob("*"):
        try:
            if f.is_file() and not f.is_symlink():
                total += f.stat().st_size
        except OSError:
            continue
    return total


def _blobs(folder: pathlib.Path) -> tuple[list[pathlib.Path], list[pathlib.Path]]:
    """``(complete blobs, unfinished blobs)`` of a model folder."""
    blobs = [b for b in (folder / "blobs").glob("*") if b.is_file()] if (folder / "blobs").is_dir() else []
    return [b for b in blobs if not b.name.endswith(".incomplete")], [b for b in blobs if b.name.endswith(".incomplete")]


def partial_downloads(cache: pathlib.Path | None = None) -> list[dict]:
    """The models whose download stopped part-way: each with the bytes already fetched and whether a finished copy of some files is there too. Read from the cache only."""
    root = pathlib.Path(cache or cache_dir())
    out = []
    for d in sorted(root.glob("models--*")) if root.is_dir() else []:
        done, unfinished = _blobs(d)
        if not unfinished:
            continue
        parts = d.name[len("models--"):].split("--")
        if len(parts) < 2:
            continue
        out.append({"repo_id": parts[0] + "/" + "--".join(parts[1:]), "partial_bytes": _size(unfinished), "partial_files": len(unfinished), "complete_bytes": _size(done)})
    return out


def discard_partial(repo_id: str, cache: pathlib.Path | None = None) -> dict:
    """Delete the unfinished files of one model (and the empty folder that leaves, when nothing finished is in it). Finished files are never touched."""
    folder = repo_folder(repo_id, cache)
    done, unfinished = _blobs(folder)
    freed = _size(unfinished)
    for f in unfinished:
        f.unlink(missing_ok=True)
    root = pathlib.Path(cache or cache_dir())
    locks = root / ".locks" / folder.name
    if locks.is_dir():
        shutil.rmtree(locks, ignore_errors=True)
    if folder.is_dir() and not done and not any((folder / "snapshots").glob("*/*")):
        shutil.rmtree(folder, ignore_errors=True)
    return {"repo_id": repo_id, "freed_bytes": freed, "files": len(unfinished)}


def removal_plan(repo_id: str, cache: pathlib.Path | None = None) -> dict:
    """What removing a model from the cache would delete: its size, how many revisions of it are there, and the unfinished part."""
    folder = repo_folder(repo_id, cache)
    if not folder.is_dir():
        raise FileNotFoundError(f"{repo_id} is not in the model cache.")
    _, unfinished = _blobs(folder)
    revisions = [p for p in (folder / "snapshots").glob("*") if p.is_dir()] if (folder / "snapshots").is_dir() else []
    return {"repo_id": repo_id, "bytes": _tree_bytes(folder), "partial_bytes": _size(unfinished), "revisions": len(revisions), "path": str(folder)}


def remove(repo_id: str, in_use=None, cache: pathlib.Path | None = None) -> dict:
    """Delete one model from the cache. ``in_use(folder)`` returns a sentence when something is using it (a download, the model server), and then nothing is deleted."""
    plan_ = removal_plan(repo_id, cache)
    folder = repo_folder(repo_id, cache)
    busy = in_use(folder) if in_use else None
    if busy:
        raise RuntimeError(busy)
    root = pathlib.Path(cache or cache_dir())
    shutil.rmtree(folder)
    shutil.rmtree(root / ".locks" / folder.name, ignore_errors=True)
    return {"repo_id": repo_id, "freed_bytes": plan_["bytes"]}


def _free_bytes(path: pathlib.Path) -> int | None:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent                              # the cache may not exist yet: the disk that will hold it is the nearest folder that does
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


def plan(repo_id: str, quant: str | None = None, api=None, cache: pathlib.Path | None = None, device_profile: dict | None = None, config_reader=None, with_files: bool = False) -> dict:
    """What downloading this model would fetch, how large it is, how much room there is, and whether it may go ahead. For a repository of GGUF files, ``recommended_quant``
    (``{quant, reason, fit}``) is the quantisation to offer first on this computer when ``device_profile`` (``device.snapshot()``) is given; the cache is counted from the model's
    own ``config.json`` when the repository has one (``config_reader(repo_id, revision)``, the Hub by default)."""
    info = hub_search._api(api).model_info(repo_id, files_metadata=True)          # the Hub refuses `expand` together with `files_metadata`
    siblings = [(s.rfilename, getattr(s, "size", None)) for s in (getattr(info, "siblings", None) or [])]
    sizes = {name: int(size) for name, size in siblings if isinstance(size, (int, float))}
    has_safetensors = any(n.endswith(".safetensors") for n in sizes)
    records = [{"rfilename": n, "size": s} for n, s in sizes.items()]
    ggufs = fit.gguf_sizes(records)
    gguf_files = fit.single_gguf_sizes(records)
    out = {"repo_id": repo_id, "revision": getattr(info, "sha", None), "gated": bool(getattr(info, "gated", False)), "token_set": token_set(), "quants": sorted(ggufs),
           "quant_bytes": dict(sorted(ggufs.items())), "needs_quant": False, "patterns": [], "files": 0, "bytes": 0, "partial_bytes": 0, "recommended_quant": None,
           "parameters": hub_search._total(info)}                  # what the Hub states for the weights (None when it states none): an install needs it to say what converting would cost
    try:
        out["partial_bytes"] = next((q["partial_bytes"] for q in partial_downloads(cache) if q["repo_id"] == repo_id), 0)
    except OSError:
        pass                                            # the cache could not be listed: the download starts as if nothing were there
    if ggufs and not has_safetensors and device_profile is not None:
        embedding = getattr(info, "pipeline_tag", None) in ("feature-extraction", "sentence-similarity")
        shape = None
        if not embedding and "config.json" in sizes:
            try:
                shape = fit.shape_from_config((config_reader or hub_search._read_config)(repo_id, out["revision"]))
            except Exception:  # ignored: without the model's own dimensions the cache is estimated and the reason says so
                shape = None
        out["recommended_quant"] = fit.recommend_quant(ggufs, None if embedding else device_profile, shape)
        if embedding:
            out["recommended_quant"]["reason"] = "A starting quantisation based on file size and precision."
    if has_safetensors:
        patterns = SAFETENSORS_PATTERNS
    elif ggufs:
        if quant is None:
            out["needs_quant"] = True
            patterns = []
        elif quant not in ggufs:
            if fit.gguf_names([{"rfilename": n} for n in sizes], quant):
                raise ValueError(f"{quant} in {repo_id} is split into several files, which this app does not download yet; choose another quantisation ({', '.join(sorted(ggufs)) or 'none is in one piece'})")
            raise ValueError(f"{repo_id} has no {quant} file; it has {', '.join(sorted(ggufs))}")
        else:
            patterns = fit.gguf_names([{"rfilename": n} for n in sizes], quant)          # by exact name: the library's patterns are case-sensitive and file names are in either case
    else:
        patterns = ["*"]
    import fnmatch
    chosen = [n for n in sizes if any(fnmatch.fnmatch(pathlib.PurePosixPath(n).name, p) or fnmatch.fnmatch(n, p) for p in patterns)] if patterns else []
    total = sum(sizes[n] for n in chosen)
    free = _free_bytes(cache or cache_dir())
    out.update(patterns=patterns, files=len(chosen), bytes=total, free_bytes=free, enough=bool(free is not None and total * MARGIN <= free) if patterns else False,
               gguf_files=[{"name": n, "bytes": gguf_files[n]} for n in sorted(chosen) if n in gguf_files])
    if with_files:                                      # the names are for the terminal of a download; the route that only asks what a download would be does not carry them
        out["file_list"] = [(n, sizes[n]) for n in chosen]
    return out


def size_text(n: int | float | None) -> str:
    """A size in words, as the page writes it: 740 kB, 6.5 MB, 1.4 GB."""
    if n is None:
        return "unknown size"
    n = float(n)
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.1f} MB"
    return f"{round(n / 1024)} kB" if n >= 1024 else f"{int(n)} B"


SHOWN_FILES = 12                    # files named in the terminal; the rest are counted


def _say_plan(say, p: dict) -> None:
    """The few lines a download starts with: what, which commit, how much, how much of it is already there, and the files."""
    say(f"Model     {p['repo_id']}")
    say(f"Revision  {p['revision'][:10]} (pinned)")
    room = "" if p.get("free_bytes") is None else f"; {size_text(p['free_bytes'])} free on the disk that holds the cache"
    say(f"Plan      {p['files']} file{'' if p['files'] == 1 else 's'}, {size_text(p['bytes'])}{room}")
    if p.get("partial_bytes"):
        say(f"Resuming  {size_text(p['partial_bytes'])} are already there")
    listed = sorted(p.get("file_list") or [], key=lambda f: -f[1])
    width = min(48, max((len(n) for n, _ in listed[:SHOWN_FILES]), default=0))
    for name, size in listed[:SHOWN_FILES]:
        say(f"  {name:<{width}}  {size_text(size):>9}")
    if len(listed) > SHOWN_FILES:
        say(f"  and {len(listed) - SHOWN_FILES} more")


def download(repo_id: str, quant: str | None = None, *, api=None, snapshot=None, cache: pathlib.Path | None = None, progress=None, say=None) -> dict:
    """Plan, refuse if it cannot be done, then fetch the pinned revision with byte progress. Returns the plan and the folder the files are in. ``say`` (a function taking one
    line) is given the lines a terminal should show before the fetch starts; the library's own bars draw nothing."""
    p = plan(repo_id, quant, api=api, cache=cache, with_files=True)
    if p["needs_quant"]:
        raise ValueError(f"{repo_id} comes only as GGUF files in several quantisations ({', '.join(p['quants'])}); choose one")
    if not p["revision"]:
        raise ValueError("the Hub did not report a commit for this model, so the download cannot be pinned")
    if p["gated"] and not p["token_set"]:
        raise ValueError(f"{repo_id} is gated: the Hub only gives it to an account that accepted its terms. Open its page on huggingface.co while signed in, accept them, make a token "
                         "there, put the token in Settings (Hugging Face token), and download again.")
    if not p["enough"]:
        have = "an unknown amount" if p.get("free_bytes") is None else f"{p['free_bytes'] / 1024 ** 3:.1f} GB"
        raise RuntimeError(f"not enough disk space: {p['bytes'] / 1024 ** 3:.1f} GB are needed (and a tenth more to spare) and {have} are free")
    if snapshot is None:
        from huggingface_hub import snapshot_download as snapshot
    if say is not None:
        _say_plan(say, p)
    kw = {"revision": p["revision"], "allow_patterns": p["patterns"], "cache_dir": str(cache or cache_dir())}
    if progress is not None:
        names = [n for n, _ in p["file_list"]]
        progress(state="downloading", label=f"Downloading {repo_id}", unit="bytes", processed=p["partial_bytes"], total=p["bytes"] or None, files=0, files_total=p["files"],
                 resumed_from=p["partial_bytes"] or None)          # the strip shows at once, before the first byte arrives
        kw["tqdm_class"] = bytes_progress(progress, p["bytes"], repo_id, files_total=p["files"], resumed_from=p["partial_bytes"], names=names)
    try:
        folder = snapshot(repo_id, **kw)
    except Exception as e:
        raise _plain(e, repo_id) from e
    return {**p, "folder": str(folder)}


def _plain(e: Exception, repo_id: str) -> Exception:
    """The Hub library's refusals as sentences a person can act on; anything else is left as it is."""
    name = type(e).__name__
    if name == "GatedRepoError":
        return ValueError(f"The Hub refused {repo_id}: it is gated and this token has no access. Accept the model's terms on its page on huggingface.co with the account the token belongs to, then download again.")
    if name in ("RepositoryNotFoundError", "RevisionNotFoundError"):
        return ValueError(f"The Hub does not show {repo_id} to this token (it does not exist, is private, or the token is wrong). Check the name and the token in Settings.")
    if name == "HfHubHTTPError" and "401" in str(e):
        return ValueError("The Hub refused the token (401). Make a new token on huggingface.co and put it in Settings.")
    return e


class _Nowhere:
    """A file that takes what is written to it and keeps nothing: the bars of the Hub library count bytes for us and draw nothing, because the terminal of the job shows
    its own lines (a library that draws three bars at once, one over the other, is what made that terminal unreadable)."""
    encoding = "utf-8"

    def write(self, text):
        return len(text)

    def flush(self):
        pass

    def isatty(self):
        return False


RATE_WINDOW = 5.0                 # seconds the speed is averaged over


def bytes_progress(report, total_bytes: int, name: str, *, files_total: int | None = None, resumed_from: int = 0, names=()):
    """A tqdm class that turns the library's byte counting into the app's progress reports (about twice a second, not on every chunk), and draws nothing itself.

    The Hub library can run several byte bars at once (one per file, or a transfer bar next to a reconstruction bar when the Xet backend is used), so adding
    their counts would count the same bytes twice. The bar whose total is the planned size (within five percent) is the authority; when none is, the bars of
    the individual files are added up.

    A report also carries the unit, the speed (bytes per second over the last five seconds), the seconds left at that speed, how many of the ``files_total`` files are finished (the
    library's own "Fetching N files" bar), what was already there when a download continued (``resumed_from``) and the file being fetched when a bar is named after one of ``names``."""
    from tqdm.auto import tqdm
    import time

    wanted = {n: n for n in names} | {n.rsplit("/", 1)[-1]: n for n in names}
    nowhere = _Nowhere()

    class Report(tqdm):
        _bars: list = []
        _last = 0.0
        _samples: list = []

        def __init__(self, *args, **kwargs):
            kwargs["file"] = nowhere
            super().__init__(*args, **kwargs)
            Report._bars.append(self)

        def close(self):
            super().close()
            if self in Report._bars:
                Report._bars.remove(self)

        @staticmethod
        def processed() -> int:
            byte_bars = [b for b in Report._bars if getattr(b, "unit", None) == "B"]
            same = [b for b in byte_bars if total_bytes and b.total and abs(b.total - total_bytes) <= 0.05 * total_bytes]
            done = max((b.n for b in same), default=sum(b.n for b in byte_bars))
            return int(min(done, total_bytes) if total_bytes else done)

        @staticmethod
        def files_done() -> int | None:
            if not files_total:
                return None
            counts = [b.n for b in Report._bars if getattr(b, "unit", None) != "B" and b.total == files_total]
            return int(min(max(counts, default=0), files_total))

        @staticmethod
        def speed(now: float, done: int) -> float | None:
            """Bytes per second over the last RATE_WINDOW seconds; None until half a second of samples exists."""
            Report._samples.append((now, done))
            while len(Report._samples) > 2 and now - Report._samples[1][0] >= RATE_WINDOW:
                Report._samples.pop(0)
            t0, d0 = Report._samples[0]
            return (done - d0) / (now - t0) if now - t0 >= 0.5 and done >= d0 else None

        def update(self, n=1):
            out = super().update(n)
            if getattr(self, "unit", None) == "B":
                now = time.monotonic()
                if now - Report._last >= 0.5:
                    Report._last = now
                    done = Report.processed()
                    rate = Report.speed(now, done)
                    state = {"state": "downloading", "label": f"Downloading {name}", "unit": "bytes", "processed": done, "total": total_bytes or None}
                    if rate and rate > 0:
                        state["rate"] = int(rate)
                        if total_bytes and done < total_bytes:
                            state["eta"] = round((total_bytes - done) / rate)
                    files = Report.files_done()
                    if files is not None:
                        state.update(files=files, files_total=files_total)
                    if resumed_from:
                        state["resumed_from"] = resumed_from
                    current = wanted.get(str(getattr(self, "desc", "") or "").strip().rstrip(":").strip())
                    if current:
                        state["current"] = current
                    report(**state)
            return out

    Report._bars = []
    Report._samples = []
    return Report
