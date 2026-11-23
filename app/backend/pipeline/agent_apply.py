"""Applying an agent session's work to the real project (agentic mode, Part C.6): the only path that ever writes to the real project tree, and deliberately never a model tool --
in Manual mode a human already approved each write when it happened, and in Auto mode nobody did, which is exactly why this step exists. Modeled directly on
``scan/update_plan.py``'s own ``apply()``/``revert()``: the same guarantees (a candidate the person reviews first, every file checked against what it was when staged before any
write, the originals kept, an all-or-nothing write that puts back what it already wrote if one file fails, an undo while the files are still as written), for a sandbox's own git
diff instead of a dependency manifest edit.

Stored under ``data/projects/<id>/agent_applies/<apply id>/`` as ``apply.json`` and ``copy/`` (the sandbox's new content of each file, read once when staged, never re-read from
the sandbox again so what is applied is exactly what was reviewed) and ``backup/`` (the real files, written only once ``apply()`` runs).

What a change is measured against is the project as it was when the copy was made (the ``aivdf-base`` tag of the sandbox's own git, see ``agent_sandbox``), so a file the owner changed in the real
project *after* the copy is told apart from one only the session changed: such a file is flagged ``drift``, and applying a candidate that contains one writes nothing at all (it would put the
session's older version over the owner's newer edit); ``refresh()`` brings the owner's edits into the sandbox first. A file the session removed is a *deletion*: listed on its own and
applied only when the person names it (``delete``), because deleting is a strictly more destructive action than overwriting content; its original is kept in the backup, so undo puts it back.
``approve_all()`` is the one call behind the page's "Approve and apply all": stage, then apply, everything or nothing.
"""
from __future__ import annotations

import datetime
import hashlib
import io
import json
import os
import pathlib
import re
import secrets
import shlex
import tarfile

from app.backend import sandbox, sandbox_files
from app.backend.pipeline import credentials, fsio, projects, util
from app.backend.pipeline.chat_tools import KEPT
from app.backend.pipeline.scan import agent_sandbox, copyin

SCHEMA = "agent_apply/1"
MAX_BYTES = 5 * 1024 * 1024
_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$")
_KINDS = {"A": "added", "M": "changed", "D": "deleted"}


def _dir(project_id: str) -> pathlib.Path:
    return projects.ROOT / project_id / "agent_applies"


def _label(root_folders: dict, root: str) -> str:
    return pathlib.PurePosixPath(root).name if len(root_folders) > 1 else ""


def _shown(label: str, rel: str) -> str:
    return f"{label}/{rel}" if label else rel


def _safe_rel(rel: str) -> bool:
    """Whether ``rel`` is a plain path inside the project: no ``..``, not absolute, no drive letter. Checked as soon as a path is read from the sandbox's own git diff (before
    anything is read or written from it) and checked again in ``_clean()`` before ``apply()``/``revert()`` ever build a real path from a stored record -- the record is read from a
    file on disk, which could in principle have been edited by hand between staging and applying."""
    parts = pathlib.PurePosixPath(str(rel).replace("\\", "/")).parts
    return bool(rel) and ".." not in parts and not pathlib.PurePosixPath(str(rel)).is_absolute() and not re.match(r"^[A-Za-z]:", str(rel))


def _target(folder: str, rel: str) -> str | None:
    """The real path ``rel`` of ``folder`` resolves to, or ``None`` when it does not actually stay inside the real, resolved folder -- checked on what the path *resolves to*, via
    ``os.path.realpath``, not just on the text of ``rel``: a symlink inside the project, or (on Windows) an 8.3 short name, can make an innocent-looking relative path land
    somewhere else entirely even once ``_safe_rel()`` has already approved its text. The one check every real file operation below goes through, never just the text check alone."""
    root = os.path.realpath(folder)
    candidate = os.path.join(root, *rel.split("/"))
    resolved = os.path.realpath(candidate)
    if resolved != root and not projects.inside(root, resolved):
        return None
    return candidate


def _read_real(folder: str, rel: str) -> bytes | None:
    path = _target(folder, rel)
    if path is None or os.path.islink(path) or not os.path.isfile(path) or os.path.getsize(path) > MAX_BYTES:
        return None
    with open(path, "rb") as f:
        return f.read()


def _blob_id(data: bytes) -> str:
    """The id git gives a file's content (``git hash-object``): lets a file of the real project be compared with the sandbox's git without asking git about it."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _base_blobs(cid: str, root: str, base: str) -> dict[str, str]:
    """``{path: blob id}`` of the project as it was at ``base``."""
    res = sandbox.exec_run(cid, f"git ls-tree -r -z {shlex.quote(base)}", workdir=root, timeout=60)
    if res["exit_code"] != 0:
        raise projects.ProjectError(f"{root} could not be read at its baseline: {res['output'].strip()[:300]}")
    out = {}
    for record in res["output"].split("\0"):
        meta, _, path = record.partition("\t")
        parts = meta.split()
        if len(parts) == 3 and path:
            out[path] = parts[2]
    return out


DRIFT_NOTE = "Your project changed this file after the copy was made, so applying would put the session's older version over your edit. Refresh the sandbox from the project first."


def diff_files(project: dict, *, keep_content: bool = False) -> list[dict]:
    """What the session's sandbox copy has changed, against the project as it was when it was copied: one entry per file, both hashes (``sha256_before`` is ``None`` for a file the real project
    does not have), a text diff where both sides decode as UTF-8, ``drift`` (the real project changed the file since the copy: see the module's note) and, for a removed file, ``can_delete``.
    Files the session made are included whether or not it checkpointed them (the sandbox's own index is brought up to date first; nothing of the real project is read for writing).
    ``keep_content`` keeps each file's new bytes under ``_new`` for ``stage`` to write out; a listing for the page never has them (they are not JSON, and not what a listing is for)."""
    doc = agent_sandbox.record(project["id"])
    cid, root_folders = doc["container"], doc["root_folders"]
    out = []
    for root in doc["roots"]:
        folder = root_folders[root]
        base = agent_sandbox.base_commit(cid, root)
        sandbox.exec_run(cid, "git add -A", workdir=root, timeout=60)
        res = sandbox.exec_run(cid, f"git diff --cached --name-status --no-renames {shlex.quote(base)}", workdir=root, timeout=60)
        if res["exit_code"] != 0:
            raise projects.ProjectError(f"{root} could not be diffed: {res['output'].strip()[:300]}")
        blobs = _base_blobs(cid, root, base)
        label = _label(root_folders, root)
        for line in res["output"].splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            kind = _KINDS.get(parts[0][:1], "changed")
            rel = parts[-1]
            if not _safe_rel(rel) or _target(folder, rel) is None:
                out.append({"file": rel, "folder": folder, "rel": rel, "label": label, "kind": kind, "sha256_before": None, "sha256_after": None, "diff": "", "drift": False,
                            "note": "This path does not stay inside the project; it is never read or applied."})
                continue
            if credentials.is_kept(rel):
                out.append({"file": _shown(label, rel), "folder": folder, "rel": rel, "label": label, "kind": kind, "sha256_before": None, "sha256_after": None, "diff": "", "drift": False, "note": KEPT})
                continue
            before = _read_real(folder, rel)
            entry = {"file": _shown(label, rel), "folder": folder, "rel": rel, "label": label, "kind": kind, "sha256_before": hashlib.sha256(before).hexdigest() if before is not None else None}
            if kind == "deleted":
                drift = before is not None and _blob_id(before) != blobs.get(rel)
                entry.update({"sha256_after": None, "diff": "", "drift": drift, "can_delete": before is not None and not drift})
                entry["note"] = DRIFT_NOTE if drift else ("Removed in the sandbox copy; it is removed from your project only if you tick it." if before is not None
                                                              else "Removed in the sandbox copy, and already gone from your project.")
                out.append(entry)
                continue
            try:
                _, new = sandbox_files.download(cid, f"{root}/{rel}")
            except sandbox_files.FileError as e:
                out.append({**entry, "sha256_after": None, "diff": "", "drift": False, "note": str(e)})
                continue
            entry["sha256_after"] = hashlib.sha256(new).hexdigest()
            if kind == "added":
                entry["drift"] = before is not None and before != new                  # the owner made a file of that name since: it would be overwritten
            else:
                entry["drift"] = before is None or (_blob_id(before) != blobs.get(rel) and before != new)         # a file that already is what the session made is not a conflict
            if entry["drift"]:
                entry["note"] = DRIFT_NOTE if before is not None else "Your project no longer has this file, which the session changed. Refresh the sandbox from the project first."
            try:
                entry["diff"] = util.udiff((before or b"").decode("utf-8") if before is not None else None, new.decode("utf-8"), f"a/{entry['file']}", f"b/{entry['file']}")
            except UnicodeDecodeError:
                entry["diff"], entry["note"] = "", "Not UTF-8 text; the content can still be applied, only shown as a diff here."
            if keep_content:
                entry["_new"] = new
            out.append(entry)
    return out


def stage(project: dict, skip: list[str] | None = None) -> dict:
    """A candidate from the session's sandbox copy right now: the new content of every changed or added file is read once and kept, so what ``apply()`` writes later is exactly
    what was reviewed, even if the sandbox keeps changing after this. ``skip`` names files the owner left out: they are not in the candidate (and so not applied, and their drift stops nothing);
    they stay in the sandbox as they are. Raises ``ProjectError`` with a sentence when there is nothing to stage."""
    projects.require_authorized(project)
    started = util.now_zulu()
    left_out = set(skip or [])
    every = diff_files(project, keep_content=True)
    files = [f for f in every if f["file"] not in left_out]
    if every and not files:
        raise projects.ProjectError("Every file the session changed was left out, so there is nothing to apply.")
    writable = [f for f in files if f["kind"] != "deleted" and "_new" in f]
    deletions = [f for f in files if f["kind"] == "deleted" and f.get("can_delete")]
    not_applied = [f for f in files if f not in writable and f not in deletions]
    if not writable and not deletions:
        raise projects.ProjectError("There is nothing changed, added or removed in the sandbox copy to apply." + (" Some files could not be read or are kept back; they are not applied here." if files else ""))
    aid = f"{datetime.datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
    folder = _dir(project["id"]) / aid
    for f in writable:
        dest = folder / "copy" / pathlib.PurePosixPath(f["file"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        fsio.write_bytes(dest, f.pop("_new"))
    record = {"schema": SCHEMA, "id": aid, "project_id": project["id"], "created": util.now_zulu(), "status": "candidate", "files": writable, "deletions": deletions,
              "conflicts": [f["file"] for f in writable + deletions if f.get("drift")], "not_applied": not_applied,
              "words": "This is a candidate made from the session's sandbox copy. Nothing in the real project has changed. Read the diff of each file; apply only the ones you agree with."}
    fsio.write_json(folder / "apply.json", record, indent=2)
    projects.audit(project["id"], tool="agent apply: stage", summary=f"{len(writable)} files and {len(deletions)} removals from the sandbox copy; a candidate, nothing in the project changed", target="agent sandbox",
                   started=started, ended=util.now_zulu(), exit_status=0)
    return record


def get(project_id: str, aid: str) -> dict:
    if not _ID.fullmatch(aid or ""):
        raise projects.NotFound("There is no such apply.")
    try:
        return json.loads((_dir(project_id) / aid / "apply.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise projects.NotFound("There is no such apply.") from None


def listing(project_id: str) -> list[dict]:
    projects.get(project_id)
    root = _dir(project_id)
    out = []
    for d in sorted(root.iterdir(), reverse=True) if root.is_dir() else []:
        try:
            a = json.loads((d / "apply.json").read_text(encoding="utf-8"))
            out.append({"id": a["id"], "created": a["created"], "status": a["status"], "files": len(a["files"]), "deletions": len(a.get("deletions", [])), "applied_at": a.get("applied_at")})
        except (OSError, ValueError, KeyError):
            continue
    return out


def _clean(a: dict) -> dict:
    """An apply record whose file names are all plain relative paths; one that is not (a record edited by hand, or one the staging step itself should already have kept out) is
    refused before any real path is built from it."""
    for f in a.get("files", []) + a.get("deletions", []):
        if not _safe_rel(f.get("file", "")) or not _safe_rel(f.get("rel", "")):
            raise projects.ProjectError("This apply record names a file outside the project, so it is not used. Stage again.")
    return a


def _check(f: dict, expect: str) -> str | None:
    raw = _read_real(f["folder"], f["rel"])
    want = f[expect]
    got = hashlib.sha256(raw).hexdigest() if raw is not None else None
    if got != want:
        return f"{f['file']} has changed since this was staged" if expect == "sha256_before" else f"{f['file']} is not the file this wrote"
    return None


def apply(project: dict, aid: str, confirmed: list[str], delete: list[str] | None = None) -> dict:
    """Write the candidate's files into the project's real folders. ``confirmed`` must be exactly the list of files the candidate names (the page shows it to the person first); ``delete`` names the
    removals the person ticked (a removal that is not named is left alone). Everything is checked before anything is written: a file changed in the project after the copy (``drift``) or after
    the candidate was staged stops the whole apply; the originals are kept; a write that fails puts back what was already written."""
    projects.require_authorized(project)
    a = _clean(get(project["id"], aid))
    if a["status"] != "candidate":
        raise projects.ProjectError("This apply has already been applied or undone; stage a new one.")
    if sorted(confirmed or []) != sorted(f["file"] for f in a["files"]):
        raise projects.ProjectError("The files you confirmed are not the files in this candidate. Read the list of files again and confirm it.")
    removals = {f["file"]: f for f in a.get("deletions", [])}
    unknown = [d for d in (delete or []) if d not in removals]
    if unknown:
        raise projects.ProjectError("These are not removals of this candidate: " + ", ".join(unknown) + ".")
    chosen = [removals[d] for d in dict.fromkeys(delete or [])]
    conflicts = [f["file"] for f in a["files"] + chosen if f.get("drift")]
    if conflicts:
        raise projects.ProjectError("Nothing was written. Your project changed " + ", ".join(conflicts) + " after the copy was made, and the session changed it too: applying would put the session's older version over your edit. "
                                    "Refresh the sandbox from the project, or discard the changes.")
    every = a["files"] + chosen
    targets = {f["file"]: _target(f["folder"], f["rel"]) for f in every}
    if any(t is None for t in targets.values()):
        raise projects.ProjectError("Nothing was written. A path does not stay inside the project any more. Stage a new candidate from the sandbox.")
    problems = [p for p in (_check(f, "sha256_before") for f in every) if p]
    if problems:
        raise projects.ProjectError("Nothing was written. " + "; ".join(problems) + ". Stage a new candidate from the sandbox.")
    folder = _dir(project["id"]) / aid
    started = util.now_zulu()
    written, removed = [], []
    try:
        for f in a["files"]:
            new = (folder / "copy" / pathlib.PurePosixPath(f["file"])).read_bytes()
            old = _read_real(f["folder"], f["rel"])
            backup = folder / "backup" / pathlib.PurePosixPath(f["file"])
            if old is not None:
                backup.parent.mkdir(parents=True, exist_ok=True)
                fsio.write_bytes(backup, old)
            fsio.write_bytes(targets[f["file"]], new)
            written.append((f, old))
        for f in chosen:
            old = _read_real(f["folder"], f["rel"])
            backup = folder / "backup" / pathlib.PurePosixPath(f["file"])
            backup.parent.mkdir(parents=True, exist_ok=True)
            fsio.write_bytes(backup, old)
            pathlib.Path(targets[f["file"]]).unlink()
            removed.append((f, old))
    except OSError as e:
        for f, old in written + removed:
            if old is not None:
                fsio.write_bytes(targets[f["file"]], old)
            else:
                try:
                    pathlib.Path(targets[f["file"]]).unlink()
                except OSError:
                    pass
        raise projects.ProjectError(f"A file could not be written ({e.strerror or type(e).__name__}); the files already written were put back and nothing changed.") from None
    a["status"], a["applied_at"], a["deleted"] = "applied", util.now_zulu(), [f["file"] for f in chosen]
    fsio.write_json(folder / "apply.json", a, indent=2)
    projects.audit(project["id"], tool="agent apply: apply", summary=f"wrote {len(a['files'])} files and removed {len(chosen)}: {', '.join(f['file'] for f in every)}"[:300],
                   target=", ".join(sorted({f["folder"] for f in every})), started=started, ended=util.now_zulu(), exit_status=0)
    return a


def approve_all(project: dict, delete: list[str] | None = None, reviewed: list[dict] | None = None, skip: list[str] | None = None) -> dict:
    """Stage what the sandbox copy has changed right now, except the files named in ``skip`` (the ones the owner unticked), and apply all of it, in one step: the page's "Approve and apply all". The candidate is made and applied from the same read; if it cannot be
    applied (a file drifted, one changed since) nothing is written and the candidate is not kept. ``reviewed`` is what the person looked at, ``[{file, sha256_after}]``: when the sandbox no longer
    has exactly those files with exactly that content (a session kept working after the page was drawn) nothing is written either, since what would be applied is not what was approved."""
    a = stage(project, skip)
    try:
        if reviewed is not None:
            now = {f["file"]: f["sha256_after"] for f in a["files"]}
            seen = {str(r.get("file")): r.get("sha256_after") for r in reviewed}
            if now != seen:
                differs = sorted({k for k in now.keys() | seen.keys() if now.get(k) != seen.get(k)})
                raise projects.ProjectError(f"Nothing was written. The sandbox changed after you looked at it ({', '.join(differs[:5])}{', ...' if len(differs) > 5 else ''}). Look at the changes again, then apply.")
        return apply(project, a["id"], [f["file"] for f in a["files"]], delete)
    except projects.ProjectError:
        discard(project["id"], a["id"])
        raise


def revert(project: dict, aid: str) -> dict:
    """Put the originals back, while the files are still exactly as this apply wrote them. A file that did not exist in the real project before (an added file) is removed, and a file this apply
    removed is put back (if it is still absent)."""
    projects.require_authorized(project)
    a = _clean(get(project["id"], aid))
    if a["status"] != "applied":
        raise projects.ProjectError("Only an applied candidate can be undone.")
    gone = [f for f in a.get("deletions", []) if f["file"] in a.get("deleted", [])]
    every = a["files"] + gone
    targets = {f["file"]: _target(f["folder"], f["rel"]) for f in every}
    if any(t is None for t in targets.values()):
        raise projects.ProjectError("Nothing was changed. A path does not stay inside the project any more. Restore from the apply's backup folder by hand if you want them.")
    problems = [p for p in (_check(f, "sha256_after") for f in a["files"]) if p]
    problems += [f"{f['file']} exists again, so it was not put back" for f in gone if _read_real(f["folder"], f["rel"]) is not None]
    if problems:
        raise projects.ProjectError("Nothing was changed. " + "; ".join(problems) + ", so undoing it here could lose your edits. Restore from the apply's backup folder by hand if you want them.")
    folder = _dir(project["id"]) / aid
    started = util.now_zulu()
    for f in every:
        backup = folder / "backup" / pathlib.PurePosixPath(f["file"])
        target = targets[f["file"]]
        if backup.is_file():
            fsio.write_bytes(target, backup.read_bytes())
        else:
            try:
                pathlib.Path(target).unlink()
            except OSError:
                pass
    a["status"], a["reverted_at"] = "reverted", util.now_zulu()
    fsio.write_json(folder / "apply.json", a, indent=2)
    projects.audit(project["id"], tool="agent apply: undo", summary=f"put back {len(every)} files", target=", ".join(sorted({f["folder"] for f in every})),
                   started=started, ended=util.now_zulu(), exit_status=0)
    return a


def discard(project_id: str, aid: str) -> None:
    """Remove a candidate's own record (``copy/`` and ``apply.json``) without touching the real project: the sandbox itself is untouched, so staging again later sees the same
    diff (or more, if the session kept working)."""
    a = get(project_id, aid)
    if a["status"] != "candidate":
        raise projects.ProjectError("Only a candidate that was never applied can be discarded.")
    import shutil
    shutil.rmtree(_dir(project_id) / aid, ignore_errors=True)


def refresh(project: dict) -> dict:
    """Bring into the sandbox copy what the owner changed in the real project after the copy was made, and make that the new baseline. For each file: where only the owner changed it, the
    owner's version goes into the sandbox; where only the session did, nothing changes; where both did (and they differ) the file is left as the session has it and listed as a conflict, to be
    settled by hand. The state before is committed as a checkpoint first, so nothing the session did is lost. Returns ``{"updated": [...], "conflicts": [...]}`` (file names as shown)."""
    projects.require_authorized(project)
    doc = agent_sandbox.record(project["id"])
    cid, root_folders = doc["container"], doc["root_folders"]
    agent_sandbox.checkpoint(project["id"], "Before refreshing from the project")
    updated, conflicts = [], []
    for root in doc["roots"]:
        folder = root_folders[root]
        base = agent_sandbox.base_commit(cid, root)
        label = _label(root_folders, root)
        arc = pathlib.PurePosixPath(root).name
        data, _summary = copyin.copy_stream(pathlib.Path(folder), arc, uid=agent_sandbox.UID)
        real: dict[str, bytes] = {}
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            for m in tar.getmembers():
                rel = m.name[len(arc) + 1:]
                if m.isfile() and rel and _safe_rel(rel) and not credentials.is_kept(rel):
                    real[rel] = tar.extractfile(m).read()
        blobs = _base_blobs(cid, root, base)
        head = _base_blobs(cid, root, "HEAD")
        take_in, drop = {}, []
        for rel, content in real.items():
            theirs = _blob_id(content)
            if theirs == blobs.get(rel):
                continue                                                         # the owner did not change it
            mine = head.get(rel)
            if mine == blobs.get(rel):
                take_in[rel] = content                                           # only the owner changed it (or added it)
            elif mine != theirs:
                conflicts.append(_shown(label, rel))
        for rel in blobs:
            if rel not in real and not credentials.is_kept(rel) and _safe_rel(rel) and copyin.skip_reason(pathlib.PurePosixPath(rel).name) is None:
                if head.get(rel) == blobs[rel]:
                    drop.append(rel)                                             # the owner removed it and the session did not touch it
                else:
                    conflicts.append(_shown(label, rel))
        if not take_in and not drop:
            continue
        for rel, content in take_in.items():
            sandbox.exec_run(cid, f"mkdir -p -- {shlex.quote(posixpath_dir(f'{root}/{rel}'))}", timeout=30)
            sandbox_files.upload(cid, posixpath_dir(f"{root}/{rel}"), pathlib.PurePosixPath(rel).name, content, overwrite=True, uid=agent_sandbox.UID)
        for rel in drop:
            sandbox.exec_run(cid, f"rm -f -- {shlex.quote(f'{root}/{rel}')}", timeout=30)
        index = "/work/.agent/baseline.idx"
        steps = [f"mkdir -p {posixpath_dir(index)}", f"export GIT_INDEX_FILE={index}", f"git read-tree {shlex.quote(base)}"]
        steps += [f"git update-index --add --cacheinfo 100644,$(git hash-object -w -- {shlex.quote(rel)}),{shlex.quote(rel)}" for rel in take_in]
        steps += [f"git update-index --force-remove -- {shlex.quote(rel)}" for rel in drop]
        steps += [f"c=$(git commit-tree $(git write-tree) -p {shlex.quote(base)} -m {shlex.quote('The project as it is now (refreshed from the project)')})", f"git tag -f {agent_sandbox.BASE_TAG} $c > /dev/null",
                  "unset GIT_INDEX_FILE", f"rm -f {index}", "git add -A", f"git commit -q -m {shlex.quote('Refreshed from the project')} --allow-empty"]
        res = sandbox.exec_run(cid, " && ".join(steps), workdir=root, timeout=120)
        if res["exit_code"] != 0:
            raise projects.ProjectError(f"{root} could not be refreshed from the project: {res['output'].strip()[:300]}")
        updated += [_shown(label, rel) for rel in [*take_in, *drop]]
    agent_sandbox.touch(project["id"])
    projects.audit(project["id"], tool="agent apply: refresh", summary=f"{len(updated)} files brought into the sandbox copy from the project; {len(conflicts)} changed on both sides and left as they were"[:300],
                   target="agent sandbox", started=util.now_zulu(), ended=util.now_zulu(), exit_status=0)
    return {"updated": updated, "conflicts": conflicts, "summary": f"Brought {len(updated)} file{'s' if len(updated) != 1 else ''} from your project into the sandbox copy" + (f"; {len(conflicts)} changed on both sides and were left as the session has them" if conflicts else "") + "."}


def posixpath_dir(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else "."
