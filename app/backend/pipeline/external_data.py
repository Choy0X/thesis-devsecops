"""Training data that did not come from this app: imported as its own dataset, labelled external, never merged into a frozen version.

A person brings a file (JSONL, Parquet or CSV) of training examples. Each row is one view of the training format: a ``task``, the ``context_text`` the model reads, and the
``response`` it should write. The response is checked against the same contract the app's own data meets (``training/finetune/contracts.py``), row by row, and every problem is
said in words with its row number; a file with a problem is not imported at all. A good file becomes a dataset folder next to the app's own (``data/processed/dataset_ext_<name>``)
with the files a run reads (``views.parquet``, ``records.parquet``, ``manifest.json``) and ``external.json``, which records where it came from: the original file name, its size
and SHA-256, the time and the person who imported it. Nothing in it is described as reviewed or verified: it has no review tier, its dataset status is ``external``, and a run on it
records that (``external_data`` in ``run_config.json``), so that it is never compared with a run on a frozen version as if nothing differed.

Columns (CSV and Parquet) or keys (JSONL):

* ``task`` (required): one of the tasks of the response contract.
* ``context_text`` (required): what the model is shown before it answers.
* ``response`` (required): the answer, as a JSON object (JSONL, Parquet) or a JSON string (CSV); for the plain-text task, the text itself. ``response_json`` is accepted as the name.
* ``split`` (``train`` or ``val``, default ``train``); ``val_role`` (``selection`` or ``calibration``, default ``selection``, for ``val`` rows only).
* ``family_id`` and ``record_id``: rows of one source share them (default: each row is its own family); ``repository``: the owner of the code, so that no repository dominates
  (default: none); ``pool`` (``supported``, ``scoped_negative`` or ``insufficient_context``, default from the answer's status); ``weight`` (above 0, default 1);
  ``language`` and ``ecosystem`` (for the breakdowns of results).
"""
from __future__ import annotations

import csv
import getpass
import hashlib
import json
import os
import pathlib
import re
import shutil
import sys
from collections import Counter

from app.backend.pipeline import util
from training.finetune import contracts as ct

SCHEMA = "dataset-external/1"
PREFIX = "ext_"
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_ROWS = 200_000
MAX_LISTED = 50
FORMATS = (".jsonl", ".ndjson", ".parquet", ".csv")
SPLITS = ("train", "val")
VAL_ROLES = ("selection", "calibration")
POOLS = ("supported", "scoped_negative", "insufficient_context")
POOL_OF_STATUS = {"finding": "supported", "no_finding": "scoped_negative", "insufficient_context": "insufficient_context"}
CORE = ("discovery", "condition_assessment", "repair")                 # a run's sampler refuses data without a row of each (training/finetune/planning.py)
NAME = re.compile(r"^[a-z0-9][a-z0-9_]{0,39}$")
CHUNK = 1 << 20


class ExternalDataError(ValueError):
    """The file or the name cannot be used; the message is a sentence for the person."""


def processed_root() -> pathlib.Path:
    from app.backend.pipeline import versions
    return pathlib.Path(versions.PROC)


def tag_of(name: str) -> str:
    if not NAME.match(name or ""):
        raise ExternalDataError("The name is lower-case letters, digits and underscores, up to 40, and starts with a letter or a digit.")
    return PREFIX + name


def folder_of(tag: str) -> pathlib.Path:
    return processed_root() / f"dataset_{tag}"


def sha256_of(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with pathlib.Path(path).open("rb") as f:
        while block := f.read(CHUNK):
            h.update(block)
    return h.hexdigest()


# -- reading a file ---------------------------------------------------------------------------------------------------------------------------

def rows_of(path: pathlib.Path):
    """``(row number, row)`` for each row of the file, the first row being 1. A JSONL line that is not a JSON object comes back as ``(n, ExternalDataError)``: the caller reports it."""
    path = pathlib.Path(path)
    suffix = path.suffix.lower()
    if suffix not in FORMATS:
        raise ExternalDataError(f"{path.name} is not a JSONL, Parquet or CSV file.")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ExternalDataError(f"{path.name} is larger than {MAX_FILE_BYTES // 1048576} MB, which is the most that can be imported.")
    n = 0
    if suffix in (".jsonl", ".ndjson"):
        with path.open("r", encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                n += 1
                try:
                    obj = json.loads(line)
                except ValueError as e:
                    yield n, ExternalDataError(f"is not valid JSON ({str(e)[:80]})")
                    continue
                yield n, obj if isinstance(obj, dict) else ExternalDataError("is not a JSON object")
    elif suffix == ".csv":
        csv.field_size_limit(min(sys.maxsize, 1 << 30))
        with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as f:
            for row in csv.DictReader(f):
                n += 1
                yield n, {k: v for k, v in row.items() if k is not None}
    else:
        import pyarrow.parquet as pq
        for batch in pq.ParquetFile(path).iter_batches(batch_size=2000):
            for row in batch.to_pylist():
                n += 1
                yield n, row


# -- checking the rows ---------------------------------------------------------------------------------------------------------------------------

def _text(v) -> str:
    return v if isinstance(v, str) else ("" if v is None else str(v))


def _response(task: str, raw):
    """``(answer, problem)``: the answer as the contract holds it, or why it cannot be read. A JSON string (CSV) is parsed; the plain-text task takes the text as it is."""
    kind = ct.by_name(task).kind
    if kind == "text":
        text = raw if isinstance(raw, str) else None
        return (text, None) if text is not None else (None, "'response' must be the text of the answer")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as e:
            return None, f"'response' is not valid JSON ({str(e)[:80]})"
    return raw, None


def check_row(n: int, row, seen: dict[str, int]) -> tuple[dict | None, list[str]]:
    """``(view fields, problems)`` for one row. The problems are sentences that begin with the row number."""
    if isinstance(row, ExternalDataError):
        return None, [f"Row {n} {row}."]
    problems: list[str] = []
    fail = lambda text: problems.append(f"Row {n}: {text}.")
    task = _text(row.get("task")).strip()
    if task not in ct.names():
        fail(f"'task' is {task!r}; it must be one of {', '.join(ct.names())}")
        return None, problems
    context = _text(row.get("context_text"))
    if not context.strip():
        fail("'context_text' is empty")
    raw = row.get("response", row.get("response_json"))
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        fail("'response' is missing")
        answer = None
    else:
        answer, why = _response(task, raw)
        if why:
            fail(why)
        else:
            for p in ct.validate_response(task, answer):
                fail(f"the answer does not fit the {task} contract: {p}")
    split = _text(row.get("split")).strip().lower() or "train"
    if split not in SPLITS:
        fail(f"'split' is {split!r}; it must be train or val")
    role = _text(row.get("val_role")).strip().lower()
    if split == "val" and role and role not in VAL_ROLES:
        fail(f"'val_role' is {role!r}; it must be selection or calibration")
    pool = _text(row.get("pool")).strip().lower()
    if pool and pool not in POOLS:
        fail(f"'pool' is {pool!r}; it must be one of {', '.join(POOLS)}")
    weight = 1.0
    raw_weight = row.get("weight")
    if raw_weight not in (None, ""):
        try:
            weight = float(raw_weight)
            if not weight > 0 or weight == float("inf"):
                raise ValueError
        except (TypeError, ValueError):
            fail(f"'weight' is {raw_weight!r}; it must be a number above 0")
    if problems:
        return None, problems
    response_json = json.dumps(answer, ensure_ascii=False, separators=(",", ":"))
    view_id = PREFIX + hashlib.sha256(f"{task}\n{context}\n{response_json}".encode("utf-8")).hexdigest()[:16]
    if view_id in seen:
        fail(f"it repeats row {seen[view_id]} (the same task, context and answer)")
        return None, problems
    seen[view_id] = n
    status = answer.get("status") if isinstance(answer, dict) else None
    family = _text(row.get("family_id")).strip() or _text(row.get("record_id")).strip() or view_id
    record = _text(row.get("record_id")).strip() or family
    return {"view_id": view_id, "task": task, "subject_id": record, "record_id": record, "issue_group_id": family, "family_id": family,
            "pool": pool or POOL_OF_STATUS.get(str(status), "supported"), "split": split, "val_role": (role or "selection") if split == "val" else None, "weight": weight,
            "review_status": "external", "eligible": True, "context_hash": hashlib.sha256(context.encode("utf-8")).hexdigest()[:16], "flags_json": "{}",
            "lineage_json": json.dumps({"source": "external", "row": n}), "context_text": context, "response_json": response_json, "prompt_chars": len(context),
            "completion_chars": len(response_json), "_repository": _text(row.get("repository")).strip() or None,
            "_language": _text(row.get("language")).strip() or None, "_ecosystem": _text(row.get("ecosystem")).strip() or None}, []


def check_file(path: pathlib.Path, progress=lambda done: None) -> dict:
    """Read the whole file and say what is in it: the views it would make, every problem (the first ``MAX_LISTED`` in words, all counted), and what a run on it would lack. Nothing is
    written."""
    path = pathlib.Path(path)
    views: list[dict] = []
    problems: list[str] = []
    bad_rows, total_rows, seen = 0, 0, {}
    for n, row in rows_of(path):
        total_rows = n
        if total_rows > MAX_ROWS:
            raise ExternalDataError(f"The file has more than {MAX_ROWS:,} rows, which is the most that can be imported.")
        view, issues = check_row(n, row, seen)
        if issues:
            bad_rows += 1
            problems.extend(issues)
        elif view is not None:
            views.append(view)
        if n % 5000 == 0:
            progress(n)
    tasks = Counter(v["task"] for v in views)
    splits = Counter(v["split"] for v in views)
    warnings = []
    if total_rows == 0:
        problems.append("The file has no rows.")
    missing = [t for t in CORE if not tasks.get(t)]
    if views and missing:
        warnings.append(f"There is no row for {', '.join(missing)}. Training needs at least one row of each of {', '.join(CORE)}, so a run on this data is refused until they are added.")
    if views and not splits.get("train"):
        warnings.append("No row is in the train split.")
    if views and not splits.get("val"):
        warnings.append("No row is in the val split, so a run on it has nothing to select its best state on.")
    return {"file": path.name, "format": path.suffix.lower().lstrip("."), "rows": total_rows, "valid_rows": len(views), "rows_with_problems": bad_rows,
            "problems_total": len(problems), "problems": problems[:MAX_LISTED], "warnings": warnings, "tasks": dict(tasks), "splits": dict(splits),
            "families": len({v["family_id"] for v in views}), "ok": not problems and bool(views), "_views": views}


def public(report: dict) -> dict:
    """The report without the rows themselves."""
    return {k: v for k, v in report.items() if not k.startswith("_")}


# -- importing --------------------------------------------------------------------------------------------------------------------------------------

def import_file(path: pathlib.Path, name: str, *, original_name: str | None = None, imported_by: str | None = None, source: dict | None = None, progress=lambda done: None) -> dict:
    """Make the dataset folder of a file that has no problem. Nothing is kept for a file that has one, and an existing dataset of that name is never overwritten."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    path = pathlib.Path(path)
    tag = tag_of(name)
    final = folder_of(tag)
    if final.exists():
        raise ExternalDataError(f"There is a dataset called {tag} already. Remove it first, or choose another name.")
    report = check_file(path, progress)
    if not report["ok"]:
        raise ExternalDataError(f"The file has {report['problems_total']} problem(s) and nothing was imported. " + " ".join(report["problems"][:5]))
    views = report["_views"]
    digest = sha256_of(path)
    root = processed_root()
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f".tmp-dataset_{tag}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    try:
        columns = [k for k in views[0] if not k.startswith("_")]
        pq.write_table(pa.Table.from_pylist([{k: v[k] for k in columns} for v in views]), tmp / "views.parquet")
        records: dict[str, dict] = {}
        for v in views:
            r = records.setdefault(v["record_id"], {"record_id": v["record_id"], "issue_group_id": v["issue_group_id"], "repository_identity": v["_repository"], "language": v["_language"],
                                                    "ecosystem": v["_ecosystem"], "split": v["split"], "review_status": "external"})
            for key, field in (("repository_identity", "_repository"), ("language", "_language"), ("ecosystem", "_ecosystem")):
                r[key] = r[key] or v[field]
        pq.write_table(pa.Table.from_pylist(list(records.values())), tmp / "records.parquet")
        external = {"schema": SCHEMA, "name": name, "tag": tag, "original_file": original_name or path.name, "size_bytes": path.stat().st_size, "sha256": digest,
                    "imported_at": util.now_utc(), "imported_by": imported_by or _person(), "rows": report["rows"], "views": len(views), "tasks": report["tasks"],
                    "splits": report["splits"], "families": report["families"], "warnings": report["warnings"], **({"source": source} if source else {})}
        (tmp / "external.json").write_text(json.dumps(external, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        files = {f.name: {"rows": (len(views) if f.name == "views.parquet" else len(records) if f.name == "records.parquet" else None), "sha256": sha256_of(f)}
                 for f in sorted(tmp.iterdir()) if f.name != "manifest.json"}
        manifest = {"schema": SCHEMA, "tag": tag, "dataset_status": "external", "phase": "external",
                    "counts": {"records": len(records), "views": len(views), "groups": report["families"], "duplicates": 0, "views_by_task": report["tasks"]},
                    "tier_mix": {}, "versions": {}, "blocked": {}, "files": files, "external": {k: external[k] for k in ("name", "original_file", "size_bytes", "sha256", "imported_at", "imported_by", "source") if k in external}}
        (tmp / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return {**external, "path": final.as_posix()}


def _person() -> str:
    try:
        return getpass.getuser()
    except Exception:  # ignored: a person who cannot be named is recorded as unknown
        return "unknown"


# -- uploads: a file waits in a folder of its own, under an id the server made, until it is checked and imported or discarded -----------------------------------------

def uploads_dir() -> pathlib.Path:
    return processed_root().parent / "interim" / "uploads"


def _valid_id(upload_id: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{32}", upload_id or ""))


def _upload_folder(upload_id: str) -> pathlib.Path:
    """The folder of an upload by the id the server made; any other string is refused, so a client cannot name a path."""
    if not _valid_id(upload_id):
        raise ExternalDataError("That is not the id of an upload.")
    return uploads_dir() / upload_id


class Upload:
    """A file arriving block by block. The name is only a label (the last part of it; its extension is checked); the bytes are written under a name the server makes, so nothing a client
    sends decides a path, and they go straight to disk, so a large file is not held in memory. More than ``limit`` bytes, or no bytes, refuses the upload and leaves nothing behind."""

    def __init__(self, filename: str, limit: int | None = None, folder_id: str | None = None):
        import uuid
        self.label = pathlib.PurePosixPath(filename.replace(chr(92), "/")).name or "upload"
        suffix = pathlib.PurePosixPath(self.label).suffix.lower()
        if suffix not in FORMATS:
            raise ExternalDataError(f"{self.label} is not a JSONL, Parquet or CSV file.")
        self.limit, self.size, self.hash = MAX_FILE_BYTES if limit is None else limit, 0, hashlib.sha256()
        self.id = folder_id or uuid.uuid4().hex                  # an id the server made: a fetch makes it before the file arrives, so the page can follow the job
        self.folder = _upload_folder(self.id)
        self.folder.mkdir(parents=True)
        self._out = (self.folder / f"data{suffix}").open("wb")

    def write(self, block: bytes) -> None:
        self.size += len(block)
        if self.size > self.limit:
            self.abort()
            raise ExternalDataError(f"The file is larger than {self.limit // 1048576} MB, which is the most that can be imported.")
        self._out.write(block)
        self.hash.update(block)

    def abort(self) -> None:
        self._out.close()
        shutil.rmtree(self.folder, ignore_errors=True)

    def finish(self) -> dict:
        self._out.close()
        if self.size == 0:
            self.abort()
            raise ExternalDataError("The file is empty.")
        (self.folder / "name.txt").write_text(self.label, encoding="utf-8")
        return {"upload_id": self.id, "filename": self.label, "size_bytes": self.size, "sha256": self.hash.hexdigest()}


def save_upload(stream, filename: str, limit: int | None = None) -> dict:
    """Keep the blocks of ``stream`` (an iterator of bytes) as an upload."""
    up = Upload(filename, limit)
    try:
        for block in stream:
            up.write(block)
        return up.finish()
    except BaseException:
        up.abort()
        raise


def upload_path(upload_id: str) -> pathlib.Path | None:
    if not _valid_id(upload_id):
        return None
    folder = _upload_folder(upload_id)
    return next(iter(sorted(folder.glob("data.*"))), None) if folder.is_dir() else None


def upload_name(upload_id: str) -> str | None:
    if not _valid_id(upload_id):
        return None
    try:
        return (_upload_folder(upload_id) / "name.txt").read_text(encoding="utf-8")
    except OSError:
        return None


def write_upload_source(upload_id: str, source: dict) -> None:
    """Where an upload came from when it did not come from the person's own computer (a dataset on the Hub: its name, commit and file)."""
    (_upload_folder(upload_id) / "source.json").write_text(json.dumps(source, sort_keys=True) + "\n", encoding="utf-8")


def upload_source(upload_id: str) -> dict | None:
    if not _valid_id(upload_id):
        return None
    try:
        data = json.loads((_upload_folder(upload_id) / "source.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def discard_upload(upload_id: str) -> bool:
    if not _valid_id(upload_id):
        return False
    folder = _upload_folder(upload_id)
    if not folder.is_dir():
        return False
    shutil.rmtree(folder, ignore_errors=True)
    return True


# -- the imports that exist -------------------------------------------------------------------------------------------------------------------------------

def listing() -> list[dict]:
    """The external datasets, newest first, each with its provenance."""
    out = []
    root = processed_root()
    for d in sorted(root.glob(f"dataset_{PREFIX}*")) if root.exists() else []:
        try:
            meta = json.loads((d / "external.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(meta, dict) and meta.get("schema") == SCHEMA:
            out.append({**meta, "path": d.as_posix()})
    return sorted(out, key=lambda m: m.get("imported_at", ""), reverse=True)


def remove(tag: str) -> dict:
    """Delete one imported dataset. Only a folder with this tool's own provenance file and an ``ext_`` tag is touched: a frozen version, or any other dataset, never is."""
    if not tag.startswith(PREFIX) or not NAME.match(tag[len(PREFIX):]):
        raise ExternalDataError("Only an imported dataset can be removed here.")
    d = folder_of(tag)
    try:
        meta = json.loads((d / "external.json").read_text(encoding="utf-8"))
        manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ExternalDataError(f"{tag} is not an imported dataset.")
    if meta.get("schema") != SCHEMA or manifest.get("dataset_status") != "external":
        raise ExternalDataError(f"{tag} is not an imported dataset.")
    if not d.resolve().is_relative_to(processed_root().resolve()):
        raise ExternalDataError("The dataset is not where imports are kept.")
    shutil.rmtree(d)
    return {"removed": tag, "original_file": meta.get("original_file")}
