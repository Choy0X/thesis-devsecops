"""Training data on the Hugging Face Hub: a search, the files of one dataset that could be imported, and the fetch of one file into the uploads folder.

Looking is metadata only (the search and the list of files with their sizes and licence). Fetching happens only when the person asks for one named file, after the interface
has shown its name, size and licence; the file arrives as an upload, so it goes through exactly the checks of a file the person chose (``external_data``): every row against the
response contract, in words, and nothing is imported from a file with a problem. The commit of the dataset and the path of the file are kept as the import's source. The Hub
client is passed in so the tests run offline.
"""
from __future__ import annotations

import json
import math
import pathlib
import re
import shutil
import tempfile

from app.backend.pipeline import external_data as ed

SEARCH_FIELDS = ["downloads", "likes", "gated", "tags", "lastModified"]
DATA_SUFFIXES = tuple(ed.FORMATS)
REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}(/[A-Za-z0-9][A-Za-z0-9._-]{0,95})?$")
REVISION = re.compile(r"^[0-9a-f]{40}$")
MAX_FILES = 200


def _api(api=None):
    if api is not None:
        return api
    from huggingface_hub import HfApi
    return HfApi()                                   # the token, when one is set, is read from HF_TOKEN by the library itself


def check_repo(repo_id: str) -> str:
    if not isinstance(repo_id, str) or not REPO.match(repo_id) or ".." in repo_id:
        raise ed.ExternalDataError("That is not the name of a Hugging Face dataset (owner/name).")
    return repo_id


def licence_of(tags) -> str | None:
    """The licence the Hub lists for a dataset (its ``license:`` tag), or None when it states none."""
    for t in tags or []:
        if isinstance(t, str) and t.startswith("license:"):
            return t[len("license:"):]
    return None


def _when(value) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


def search(query: str = "", limit: int = 20, api=None) -> dict:
    """The most downloaded datasets matching ``query``, as plain rows. Nothing is downloaded."""
    limit = max(1, min(int(limit), 50))
    found = _api(api).list_datasets(search=query.strip() or None, sort="downloads", limit=limit, expand=SEARCH_FIELDS)
    rows = [{"id": d.id, "downloads": getattr(d, "downloads", None), "likes": getattr(d, "likes", None), "gated": bool(getattr(d, "gated", False)),
             "license": licence_of(getattr(d, "tags", None)), "updated": _when(getattr(d, "last_modified", None))} for d in found]
    return {"query": query.strip(), "datasets": rows}


def files(repo_id: str, api=None) -> dict:
    """The files of one dataset that this app can import (JSONL, Parquet, CSV), smallest first, each with its size and whether it is within the import limit, and the commit they
    are listed at. A gated dataset is said to be gated: it cannot be fetched without the person's own access."""
    check_repo(repo_id)
    info = _api(api).dataset_info(repo_id, files_metadata=True)
    listed = []
    for s in getattr(info, "siblings", None) or []:
        name = getattr(s, "rfilename", "")
        size = getattr(s, "size", None)
        if name.lower().endswith(DATA_SUFFIXES) and ".." not in name.split("/") and not name.startswith("/"):
            n = int(size) if isinstance(size, (int, float)) and math.isfinite(size) else None
            listed.append({"path": name, "size_bytes": n, "within_limit": n is not None and n <= ed.MAX_FILE_BYTES})
    listed.sort(key=lambda f: (f["size_bytes"] is None, f["size_bytes"] or 0, f["path"]))
    return {"id": repo_id, "revision": getattr(info, "sha", None), "gated": bool(getattr(info, "gated", False)), "license": licence_of(getattr(info, "tags", None)),
            "limit_bytes": ed.MAX_FILE_BYTES, "total_importable": len(listed), "files": listed[:MAX_FILES]}


def fetch(repo_id: str, revision: str, path: str, upload_id: str, downloader=None, progress=lambda done, total: None) -> dict:
    """Fetch one named file of a dataset, at one commit, into the upload ``upload_id`` (an id the server made), and record where it came from. The file is downloaded into a folder
    that is thrown away and then copied in, so the Hugging Face cache gains nothing the Installed list would read, and a file over the import limit is refused."""
    check_repo(repo_id)
    if not REVISION.match(revision or ""):
        raise ed.ExternalDataError("Fetch a file at a commit of the dataset (40 characters), not at a branch name that can move.")
    clean = pathlib.PurePosixPath(path)
    if clean.is_absolute() or ".." in clean.parts or not str(clean).lower().endswith(DATA_SUFFIXES):
        raise ed.ExternalDataError("That is not a JSONL, Parquet or CSV file of the dataset.")
    folder = ed._upload_folder(upload_id)
    if folder.exists():
        raise ed.ExternalDataError("That upload exists already.")
    if downloader is None:
        from huggingface_hub import hf_hub_download as downloader
    with tempfile.TemporaryDirectory(prefix="aivdf-hubdata-") as tmp:
        got = pathlib.Path(downloader(repo_id=repo_id, filename=str(clean), revision=revision, repo_type="dataset", local_dir=tmp))
        size = got.stat().st_size
        if size > ed.MAX_FILE_BYTES:
            raise ed.ExternalDataError(f"{clean.name} is larger than {ed.MAX_FILE_BYTES // 1048576} MB, which is the most that can be imported.")
        up = ed.Upload(clean.name, folder_id=upload_id)
        try:
            with got.open("rb") as fh:
                done = 0
                while True:
                    block = fh.read(1 << 20)
                    if not block:
                        break
                    up.write(block)
                    done += len(block)
                    progress(done, size)
            out = up.finish()
        except BaseException:
            up.abort()
            raise
    ed.write_upload_source(upload_id, {"kind": "huggingface", "repo": repo_id, "revision": revision, "file": str(clean)})
    return out | {"source": ed.upload_source(upload_id)}
