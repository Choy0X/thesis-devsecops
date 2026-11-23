"""The images of the sandbox: what is on this computer with its digest, which are in use, which one verification is pinned to, building one from a Dockerfile, and pruning only what this app made.

An image is shown with the digest Docker records for it (the registry digest when it came from a registry, else its own id), its size and age, how many containers use it, whether this app made
it (the ``aivdf.image`` label, or the tag a recreated container leaves) and whether it is the pinned verification image. Pinning records the image id and tag in
``data/sandbox/pinned_image.json``; the verification runner then refuses to run on an image of that tag whose id differs, so every receipt names the same image. Building runs a Dockerfile the person
wrote, with no network for its steps unless they ask for it; the image is labelled as this app's. Pruning lists, and removes only on request, the unused images that carry this app's label or
tag, and never the pinned image or the tool image of Scan Codebase (no container keeps that one running between scans): an image of another project, tagged or not, is never offered or removed
here. (The first real look at the plan, on 2026-10-10, found 34 untagged images without this app's label, 10.8 GB, and the tool image in it; the plan used to offer every untagged image.)
"""
from __future__ import annotations

import io
import json
import pathlib
import re

from app.backend import sandbox
from app.backend.pipeline import fsio, util

IMAGE_LABEL = "aivdf.image"
PINNED = pathlib.Path("data/sandbox/pinned_image.json")
MAX_DOCKERFILE = 20_000
TAG = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,60}(:[A-Za-z0-9][A-Za-z0-9._-]{0,60})?$")


class ImageError(ValueError):
    """An image action that is refused, with the reason in words."""


def _digest(im) -> str:
    digests = im.attrs.get("RepoDigests") or []
    return digests[0].split("@", 1)[-1] if digests else (im.id or "")


def _ours(im) -> bool:
    labels = (im.attrs.get("Config") or {}).get("Labels") or {}
    from app.backend import sandbox_runtime
    if labels.get('aivdf.installation') not in (None, sandbox_runtime.state()['installation_id']):
        return False
    return labels.get(IMAGE_LABEL) == "1" or labels.get(sandbox.LABEL_KEY) == "1" or any(t.startswith("aivdf-recreate:") for t in (im.tags or []))       # a commit of a sandbox container keeps its label


def toolkit_id() -> str | None:
    """The id of the tool image of Scan Codebase, from the record its build wrote; no container keeps it running between scans, so it must never look unused."""
    from app.backend.pipeline.scan import sandbox_run
    try:
        return json.loads(pathlib.Path(sandbox_run.TOOLKIT_IMAGE).read_text(encoding="utf-8")).get("id") or None
    except (OSError, ValueError, AttributeError):
        return None


def pinned() -> dict | None:
    try:
        doc = json.loads(PINNED.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get("id") else None


def listing(*, owned_only=False) -> list[dict]:
    """Every image on this computer, newest first."""
    client = sandbox._client()
    pin = pinned()
    tool = toolkit_id()
    used: dict[str, int] = {}
    for ct in client.containers.list(all=True):
        used[ct.attrs.get("Image", "")] = used.get(ct.attrs.get("Image", ""), 0) + 1
    out = []
    for im in client.images.list(all=False):
        if owned_only and not _ours(im):
            continue
        out.append({"id": im.short_id, "full_id": im.id, "tags": list(im.tags or []), "digest": _digest(im), "size_mb": round(im.attrs.get("Size", 0) / 1e6, 1),
                    "created": (im.attrs.get("Created") or "")[:19], "containers": used.get(im.id, 0), "ours": _ours(im), "dangling": not im.tags,
                    "pinned": bool(pin and pin.get("id") == im.id), "toolkit": bool(tool and tool == im.id)})
    out.sort(key=lambda r: r["created"], reverse=True)
    return out


def pin(reference: str) -> dict:
    """Make this image the one verification runs on. It must be on this computer."""
    try:
        im = sandbox._client().images.get(reference)
    except Exception as e:
        raise ImageError(f"{reference} is not on this computer: pull it first ({type(e).__name__}).") from e
    tag = reference if reference in (im.tags or []) else (im.tags[0] if im.tags else None)
    if not tag:
        raise ImageError("An image with no tag cannot be pinned: tag it first.")
    doc = {"tag": tag, "id": im.id, "digest": _digest(im), "pinned_at": util.now_zulu()}
    fsio.write_json(PINNED, doc, indent=2)
    return doc


def unpin() -> dict:
    PINNED.unlink(missing_ok=True)
    return {"pinned": None}


def check_pin(reference: str, image_id: str) -> str | None:
    """For the verification runner: a sentence when ``reference`` is the pinned tag but ``image_id`` is not the pinned image, else None."""
    pin_ = pinned()
    if pin_ and pin_.get("tag") == reference and pin_.get("id") != image_id:
        return (f"{reference} is pinned to {str(pin_['id'])[:19]} but the image on this computer is {str(image_id)[:19]}. Pull the pinned image again, or change the pin on the Images tab.")
    return None


def build(dockerfile: str, tag: str, network: bool = False, on_line=lambda line: None) -> dict:
    """Build an image from Dockerfile text. The steps have no network unless ``network``; the image carries this app's label. Returns the image and the last lines of the log."""
    if not isinstance(dockerfile, str) or not dockerfile.strip():
        raise ImageError("The Dockerfile is empty.")
    if len(dockerfile) > MAX_DOCKERFILE:
        raise ImageError(f"The Dockerfile is longer than {MAX_DOCKERFILE:,} characters.")
    if not TAG.match(tag or ""):
        raise ImageError("The tag is lower-case letters, digits and . _ / - with an optional :version, such as aivdf/tools:1.")
    client = sandbox._client()
    log: list[str] = []
    image = None
    from app.backend import sandbox_runtime
    image_labels = {IMAGE_LABEL: '1', 'aivdf.installation': sandbox_runtime.state()['installation_id'], 'aivdf.image.tags': json.dumps([tag])}
    for chunk in client.api.build(fileobj=io.BytesIO(dockerfile.encode("utf-8")), tag=tag, rm=True, forcerm=True, pull=False, labels=image_labels,
                                  network_mode="default" if network else "none", decode=True):
        line = (chunk.get("stream") or chunk.get("status") or "").strip()
        if chunk.get("error"):
            raise ImageError(f"The build failed: {str(chunk['error']).strip()[:400]}")
        if line:
            log.append(line)
            on_line(line)
    try:
        image = client.images.get(tag)
    except Exception as e:
        raise ImageError("The build ended without making an image.") from e
    return {"tag": tag, "id": image.short_id, "digest": _digest(image), "size_mb": round(image.attrs.get("Size", 0) / 1e6, 1), "network": network, "log_tail": log[-15:]}


def prune_plan() -> dict:
    """What pruning would remove: images this app made that no container uses, except the pinned verification image and the tool image of Scan Codebase. An untagged image without this app's
    label is not offered: it may belong to other work."""
    rows = listing()
    pin_id = (pinned() or {}).get("id")
    candidates = [r for r in rows if r["containers"] == 0 and r["ours"] and r["full_id"] != pin_id and not r["toolkit"]]
    return {"images": [{k: r[k] for k in ("id", "full_id", "tags", "size_mb", "ours", "dangling", "created")} for r in candidates], "size_mb": round(sum(r["size_mb"] for r in candidates), 1)}


def prune(image_ids: list[str]) -> dict:
    """Remove the listed images, each only if it is in the plan (so a name that was not offered is never removed)."""
    plan = prune_plan()
    allowed = {r["full_id"]: r for r in plan["images"]}
    client = sandbox._client()
    removed, skipped = [], []
    for wanted in image_ids:
        row = allowed.get(wanted) or next((r for r in plan["images"] if r["id"] == wanted), None)
        if not row:
            skipped.append({"id": wanted, "why": "not an image this app made, or in use, or pinned"})
            continue
        try:
            client.images.remove(row["full_id"], force=False)
            from docker.errors import NotFound
            try:
                client.images.get(row['full_id'])
            except NotFound:
                removed.append(row['id'])
            else:
                skipped.append({'id': row['id'], 'why': 'Docker still lists the image; removal was not confirmed.'})
        except Exception as e:
            skipped.append({"id": row["id"], "why": f"{type(e).__name__}: {str(e)[:120]}"})
    return {"removed": removed, "skipped": skipped, "freed_mb": round(sum(allowed[i]["size_mb"] for i in allowed if allowed[i]["id"] in removed), 1)}
