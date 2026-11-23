"""Routes for training data that comes from outside: upload a file, say what is wrong with it, import it as a dataset of its own, list the imports and remove one. A file is
only ever read; an import never touches a frozen version (pipeline/external_data.py)."""
from __future__ import annotations

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from app.backend import errors

router = APIRouter()


@router.put("/api/data/external/upload")
async def external_upload(request: Request, filename: str):
    """Receive a JSONL, Parquet or CSV file as the request body and keep it, under an id, until it is imported or discarded. It is read as it arrives and refused beyond the size limit."""
    from app.backend.pipeline import external_data as ed
    try:
        up = ed.Upload(filename)
    except ed.ExternalDataError as e:
        return errors.error(str(e), 400)
    try:
        async for block in request.stream():
            up.write(block)
        return up.finish()
    except ed.ExternalDataError as e:
        return errors.error(str(e), 413 if "larger than" in str(e) else 400)
    except BaseException:
        up.abort()
        raise


class UploadRef(BaseModel):
    upload_id: str


@router.post("/api/data/external/check")
def external_check(body: UploadRef):
    """Read an uploaded file and say what is in it and every problem, in words with the row number. Nothing is stored."""
    from app.backend.pipeline import external_data as ed
    path = ed.upload_path(body.upload_id)
    if path is None:
        return errors.error("That upload is not there any more. Choose the file again.", 404)
    try:
        return ed.public(ed.check_file(path)) | {"filename": ed.upload_name(body.upload_id)}
    except ed.ExternalDataError as e:
        return errors.error(str(e), 400)


class ImportIn(BaseModel):
    upload_id: str
    name: str


@router.post("/api/data/external")
def external_import(body: ImportIn):
    """Import an uploaded file as a dataset of its own, as a job with progress. The name is checked and must be free; the file's problems are the job's failure."""
    from app.backend import jobs
    from app.backend.pipeline import external_data as ed
    try:
        tag = ed.tag_of(body.name)
    except ed.ExternalDataError as e:
        return errors.error(str(e), 400)
    if ed.upload_path(body.upload_id) is None:
        return errors.error("That upload is not there any more. Choose the file again.", 404)
    if ed.folder_of(tag).exists():
        return errors.error(f"There is a dataset called {tag} already. Remove it first, or choose another name.", 409)
    try:
        job = jobs.start("import_external", {"upload": body.upload_id, "name": body.name})
    except jobs.TooManyJobs as e:
        return errors.error(str(e), 409)
    return {"id": job.id, "stage": job.stage, "tag": tag}


@router.delete("/api/data/external/upload/{upload_id}")
def external_discard(upload_id: str):
    from app.backend.pipeline import external_data as ed
    return {"discarded": ed.discard_upload(upload_id)}


@router.get("/api/data/external")
def external_list():
    """The imported datasets with where each came from: the original file name, size, SHA-256, the time and the person."""
    from app.backend.pipeline import external_data as ed
    return {"imports": ed.listing()}


@router.delete("/api/data/external/{tag}")
def external_remove(tag: str):
    """Remove one imported dataset. A frozen version, or any dataset the app built, cannot be removed here."""
    from app.backend.pipeline import external_data as ed
    try:
        return ed.remove(tag)
    except ed.ExternalDataError as e:
        return errors.error(str(e), 400)


# -- datasets on the Hugging Face Hub: look (metadata only), then fetch one named file into an upload, which goes through the checks above ------------------------------------

def _hub_unreachable(e: Exception, what: str):
    return errors.error(f"Hugging Face could not be reached, so {what}.", 502, detail=f"{type(e).__name__}: {str(e)[:200]}", hint="Check the internet connection. Your own files can still be imported.")


@router.get("/api/data/hub/search")
def hub_search(q: str = "", limit: int = Query(20, ge=1, le=50)):
    """Datasets on the Hub, the most downloaded first. Metadata only; nothing is downloaded. When the Hub cannot be reached the answer says so, never an empty list that looks like "none"."""
    from app.backend.pipeline import hub_datasets as hd
    try:
        return hd.search(q, limit)
    except Exception as e:  # ignored: any failure to reach the Hub is turned into one plain answer
        return _hub_unreachable(e, "the search has no answer")


@router.get("/api/data/hub/files")
def hub_files(repo: str):
    """The files of one dataset that can be imported, with their sizes, the licence the Hub lists and the commit they are listed at. Metadata only."""
    from app.backend.pipeline import external_data as ed, hub_datasets as hd
    try:
        return hd.files(repo)
    except ed.ExternalDataError as e:
        return errors.error(str(e), 400)
    except Exception as e:  # ignored: any failure to reach the Hub is turned into one plain answer
        return _hub_unreachable(e, "its files cannot be listed")


class HubFetch(BaseModel):
    repo: str
    revision: str
    path: str


@router.post("/api/data/hub/fetch")
def hub_fetch(body: HubFetch):
    """Fetch one named file of a dataset at one commit, as a job with progress. The file becomes an upload (its id is returned at once), and is then checked and imported like a file of
    the person's own. The interface shows the file's name, size and licence before this is called."""
    import uuid

    from app.backend import jobs
    from app.backend.pipeline import external_data as ed, hub_datasets as hd
    try:
        hd.check_repo(body.repo)
    except ed.ExternalDataError as e:
        return errors.error(str(e), 400)
    if not hd.REVISION.match(body.revision) or not body.path.lower().endswith(hd.DATA_SUFFIXES) or ".." in body.path.split("/") or body.path.startswith("/"):
        return errors.error("Fetch a JSONL, Parquet or CSV file of the dataset at a commit of 40 characters.", 400)
    upload_id = uuid.uuid4().hex
    try:
        job = jobs.start("fetch_hub_dataset", {"repo": body.repo, "revision": body.revision, "path": body.path, "upload": upload_id})
    except jobs.TooManyJobs as e:
        return errors.error(str(e), 409)
    return {"id": job.id, "stage": job.stage, "upload_id": upload_id}
