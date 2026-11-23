"""Update the dependencies of a project: a plan from the dependency scan, a candidate made on a copy of the manifests, a prediction of what the scan would say afterwards, and an
apply step that writes the manifests of the real folders only after the person has confirmed the list of files, with a backup and an undo.

What this does and does not do:

* It edits manifests (``requirements*.txt``, ``pyproject.toml``, ``package.json``) as text, keeping their layout, line endings and every other line. Lock files are never edited by
  this module, because only the package manager can write them; the plan says which ones need to be regenerated.
* Nothing is installed, nothing of the project is run, no test is run, and no network is used (the newer releases come from the dependency scan that looked them up). So the
  prediction is advisory matching of the target versions against the advisories on this computer, nothing more: it does not say that the project still works after the update, and
  a candidate is never called safe, correct or ready to merge.
* Applying checks that the files are still the ones the plan was made from, keeps the originals under the update's folder, and can be undone while the files are still as written.

Stored under ``data/projects/<id>/updates/<update id>/`` as ``update.json``, ``original/``, ``copy/``, ``backup/`` and ``patch.diff``.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import pathlib
import re
import secrets
import shutil

from app.backend.pipeline import credentials, fsio, projects, util
from app.backend.pipeline.scan import advisories as adv, manifests as mf, project_deps

SCHEMA = "project-update/1"
MODES = {"security": "Security fixes only", "all": "Everything that is outdated"}
LOCK_FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock"}
MAX_BYTES = 2 * 1024 * 1024
_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$")
_VERSION = r"[0-9][0-9A-Za-z.+!_\-]*"
PLAIN_VERSION = re.compile(r"[0-9][0-9A-Za-z.+!_\-]{0,63}")


def editable(name: str) -> bool:
    low = name.lower()
    return low == "pyproject.toml" or low == "package.json" or (low.endswith(".txt") and (low.startswith("requirements") or low.startswith("constraints")))


def _pkg(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.lower())


# -- editing one manifest as text ------------------------------------------------------------------------------------------------------------------------------

def _replace_version(spec: str, target: str) -> tuple[str | None, str | None]:
    """``(new spec, None)`` or ``(None, reason)``: the first exact or lower-bound version in a requirement's specifier becomes ``target``."""
    m = re.search(r"(===|==|~=|>=)\s*(" + _VERSION + r")", spec)
    if not m:
        return None, "only an upper bound or no version is written, so there is nothing to move"
    if "*" in spec[m.end():m.end() + 2] or spec[m.end():m.end() + 1] == "*":
        return None, "a wildcard version is left as it is"
    return spec[:m.start(2)] + target + spec[m.end(2):], None


def edit_requirements(text: str, targets: dict[str, str]) -> tuple[str, list[dict], list[dict]]:
    applied, skipped, out = [], [], []
    for n, raw in enumerate(text.splitlines(keepends=True), 1):
        body = raw.rstrip("\r\n")
        eol = raw[len(body):]
        m = re.match(r"^(\s*)([A-Za-z0-9][A-Za-z0-9_.\-]*)(\s*\[[^\]]*\])?(\s*)([^;#]*?)(\s*)((?:;[^#]*)?)(\s*(?:#.*)?)$", body)
        if body.lstrip().startswith(("#", "-")) or not m or _pkg(m.group(2)) not in targets:
            out.append(raw)
            continue
        name = _pkg(m.group(2))
        new_spec, why = _replace_version(m.group(5), targets[name])
        if new_spec is None:
            skipped.append({"name": name, "line": n, "reason": why})
            out.append(raw)
            continue
        new = m.group(1) + m.group(2) + (m.group(3) or "") + m.group(4) + new_spec + m.group(6) + m.group(7) + m.group(8)
        applied.append({"name": name, "line": n, "before": body.strip(), "after": new.strip()})
        out.append(new + eol)
    return "".join(out), applied, skipped


def edit_pyproject(text: str, targets: dict[str, str]) -> tuple[str, list[dict], list[dict]]:
    applied, skipped, out = [], [], []
    for n, raw in enumerate(text.splitlines(keepends=True), 1):
        line, new, hit = raw, raw, ""
        for m in re.finditer(r"([\"'])([A-Za-z0-9][A-Za-z0-9_.\-]*)(\s*\[[^\]]*\])?([^\"']*)\1", raw):                  # "flask==2.0.1" inside a dependencies array
            name = _pkg(m.group(2))
            if name in targets and re.search(r"(===|==|~=|>=|<|>|!=)", m.group(4)) is not None:
                spec, why = _replace_version(m.group(4), targets[name])
                if spec is None:
                    skipped.append({"name": name, "line": n, "reason": why})
                else:
                    new = new.replace(m.group(0), m.group(1) + m.group(2) + (m.group(3) or "") + spec + m.group(1), 1)
                    hit = name
        pm = re.match(r"^(\s*)([A-Za-z0-9][A-Za-z0-9_.\-]*)(\s*=\s*)([\"'])([\^~><=!]*\s*)(" + _VERSION + r")([^\"']*)([\"'])(.*)$", raw.rstrip("\r\n"))   # poetry: flask = "^2.0.1"
        if pm and _pkg(pm.group(2)) in targets and pm.group(2).lower() != "python":
            new = pm.group(1) + pm.group(2) + pm.group(3) + pm.group(4) + pm.group(5) + targets[_pkg(pm.group(2))] + pm.group(7) + pm.group(8) + pm.group(9) + raw[len(raw.rstrip("\r\n")):]
            hit = _pkg(pm.group(2))
        tm = re.match(r"^(.*\bversion\s*=\s*[\"'])([\^~><=!]*\s*)(" + _VERSION + r")([\"'].*)$", raw.rstrip("\r\n"))      # poetry inline table: flask = { version = "^2.0", extras = [...] }
        nm = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9_.\-]*)\s*=\s*\{", raw)
        if tm and nm and _pkg(nm.group(1)) in targets:
            new = tm.group(1) + tm.group(2) + targets[_pkg(nm.group(1))] + tm.group(4) + raw[len(raw.rstrip("\r\n")):]
            hit = _pkg(nm.group(1))
        if new != line:
            applied.append({"name": hit, "line": n, "before": line.strip(), "after": new.strip()})
        out.append(new)
    return "".join(out), applied, skipped


_PKG_SECTIONS = ("dependencies", "devDependencies", "optionalDependencies")


def edit_package_json(text: str, targets: dict[str, str]) -> tuple[str, list[dict], list[dict]]:
    applied, skipped = [], []
    try:
        doc = json.loads(text)
    except ValueError:
        return text, [], [{"name": "", "line": 0, "reason": "the file is not valid JSON"}]
    new = text
    for section in _PKG_SECTIONS:
        block = doc.get(section)
        if not isinstance(block, dict):
            continue
        start = re.search(r'"' + section + r'"\s*:\s*\{', new)
        if not start:
            continue
        depth, i = 1, start.end()
        while i < len(new) and depth:
            depth += {"{": 1, "}": -1}.get(new[i], 0)
            i += 1
        head, body, tail = new[:start.end()], new[start.end():i - 1], new[i - 1:]
        for name, spec in block.items():
            if name not in targets:
                continue
            m = re.match(r"^([\^~]|>=|>|=)?\s*v?(" + _VERSION + r")$", str(spec))
            if not m:
                skipped.append({"name": name, "line": 0, "reason": f"{spec} is not a plain version or range, so it is left as it is"})
                continue
            old = r'("' + re.escape(name) + r'"\s*:\s*")' + re.escape(str(spec)) + r'(")'
            replacement = (m.group(1) or "") + targets[name]
            body, n = re.subn(old, lambda mm: mm.group(1) + replacement + mm.group(2), body, count=1)
            if n:
                applied.append({"name": name, "line": new[:start.start()].count("\n") + 1, "before": f'"{name}": "{spec}"', "after": f'"{name}": "{replacement}"', "section": section})
        new = head + body + tail
    return new, applied, skipped


EDITORS = {"pyproject.toml": edit_pyproject, "package.json": edit_package_json}


def edit_manifest(name: str, text: str, targets: dict[str, str]) -> tuple[str, list[dict], list[dict]]:
    """Edit one manifest. A target version is written into the file text, and it comes from advisory data and registry answers, which are not ours: only a plain version (digits,
    letters and . + ! _ -) is ever written, so a value with a quote, a comma, a newline or a space cannot add a line, a key or a dependency."""
    bad = [k for k, v in targets.items() if not PLAIN_VERSION.fullmatch(str(v))]
    skipped = [{"name": k, "line": 0, "reason": "the new version is not a plain version number, so it was not written"} for k in bad]
    new, applied, left = EDITORS.get(name.lower(), edit_requirements)(text, {k: v for k, v in targets.items() if k not in bad})
    return new, applied, left + skipped


# -- the plan --------------------------------------------------------------------------------------------------------------------------------------------------

def target_for(row: dict, mode: str, depversions) -> tuple[str | None, str | None, str | None]:
    """``(target version, kind, why not)`` for one dependency of a dependency report. kind is ``fix`` (an advisory affects the version in use) or ``newer``."""
    if not row.get("version"):
        return None, None, "the version in use is not known (only a range is declared), so there is nothing to move from"
    eco = row["ecosystem"]
    plain_adv = [a for a in row.get("advisories") or [] if PLAIN_VERSION.fullmatch(str(a.get("safe_version") or a.get("first_fixed") or "0"))]
    plain_out = PLAIN_VERSION.fullmatch(str((row.get("outdated") or {}).get("latest") or "0")) is not None
    untrusted = len(plain_adv) != len(row.get("advisories") or []) or not plain_out                    # a version string that is not plain is not trusted
    row = {**row, "advisories": plain_adv, "outdated": row.get("outdated") if plain_out else {}}
    fixes = [a.get("safe_version") or a.get("first_fixed") for a in row.get("advisories") or []]
    fixes = [f for f in fixes if f]
    out = row.get("outdated") or {}
    latest = out.get("latest") if out.get("state") == "outdated" else None
    best = None
    if mode == "security":
        if row["state"] != "vulnerable":
            return None, None, None
        if not fixes:
            return None, None, ("the fixed version in the data is not a plain version number, so it was not used" if untrusted else
                                "an advisory affects it but no fixed release is known: replace the package or mitigate")
        best, kind = max(fixes, key=lambda v: depversions.VKey(eco, v)), "fix"
    else:
        cands = [v for v in [latest, *(fixes if row["state"] == "vulnerable" else [])] if v]
        if not cands:
            return None, None, ("the fixed or latest version in the data is not a plain version number, so it was not used" if untrusted else None)
        best, kind = max(cands, key=lambda v: depversions.VKey(eco, v)), ("fix" if row["state"] == "vulnerable" else "newer")
    try:
        if not depversions.VKey(eco, row["version"]) < depversions.VKey(eco, best):
            return None, None, None
    except Exception:  # ignored: versions that cannot be compared are not moved
        return None, None, "the versions cannot be compared"
    return best, kind, None


def declaring(project: dict, folder: str, ecosystem: str, name: str) -> list[str]:
    """The editable manifests of a folder of the project that declare a package. The report lists the file that settled the version in use (a lock file, when there is one), which is
    not the file to edit, so the manifests are read again here."""
    found, root = [], pathlib.Path(folder)
    for f in project["folders"]:
        if f["path"] != folder:
            continue
        for rels in (f.get("manifests") or {}).values():
            for rel in rels:
                if not editable(pathlib.PurePosixPath(rel).name):
                    continue
                try:
                    deps = mf.read(root, rel)
                except mf.ManifestError:
                    continue
                if any(d["ecosystem"] == ecosystem and (_pkg(d["name"]) == _pkg(name) if ecosystem == "PyPI" else d["name"] == name) for d in deps):
                    found.append(rel)
    return found


def build_plan(project: dict, report: dict, mode: str, depversions) -> dict:
    changes, unfixed = [], []
    for row in report["dependencies"]:
        to, kind, why = target_for(row, mode, depversions)
        if why:
            unfixed.append({"ecosystem": row["ecosystem"], "name": row["name"], "version": row.get("version"), "reason": why})
        if not to:
            continue
        files = declaring(project, row["folder"], row["ecosystem"], row["name"])
        locks = [m for m in row["manifests"] if pathlib.PurePosixPath(m).name in LOCK_FILES]
        changes.append({"ecosystem": row["ecosystem"], "name": row["name"], "from": row["version"], "to": to, "kind": kind,
                        "bump": project_deps._bump(row["version"], to, row["ecosystem"], depversions) or "newer", "folder": row["folder"],
                        "advisories": [a["id"] for a in row.get("advisories") or []], "files": files, "locks": locks})
    return {"mode": mode, "changes": changes, "not_updated": unfixed}


# -- the candidate ---------------------------------------------------------------------------------------------------------------------------------------------

def _dir(project_id: str) -> pathlib.Path:
    return projects.ROOT / project_id / "updates"


def _label(project: dict, folder: str) -> str:
    folders = [f["path"] for f in project["folders"]]
    base = os.path.basename(os.path.realpath(folder).rstrip("\\/")) or "project"
    return base if len(folders) > 1 else ""


def _shown(label: str, rel: str) -> str:
    return f"{label}/{rel}" if label else rel


def _read_manifest(folder: str, rel: str) -> bytes | None:
    root = os.path.realpath(folder)
    path = os.path.join(root, *rel.split("/"))
    if not projects.inside(root, path) or os.path.islink(path) or not os.path.isfile(path) or credentials.is_kept(rel) or os.path.getsize(path) > MAX_BYTES:
        return None
    with open(path, "rb") as f:
        return f.read()


def make(project: dict, mode: str = "security", *, scan_id: str | None = None, advisories=adv.by_package, progress=lambda *a, **k: None) -> dict:
    """Make a candidate update from the newest dependency scan of the project; nothing in the project is changed. Raises ProjectError with a sentence when it cannot."""
    from app.backend.pipeline.ingest import depversions
    projects.require_authorized(project)
    if mode not in MODES:
        raise projects.ProjectError("The mode is security fixes only, or everything that is outdated.")
    scans = [s for s in projects.scans(project["id"]) if s["kind"] == "dependencies"]
    if scan_id:
        scans = [s for s in scans if s["scan_id"] == scan_id]
    if not scans:
        raise projects.ProjectError("Scan the dependencies of the project first: an update is planned from that scan.")
    report = projects.scan_report(project["id"], scans[0]["scan_id"])
    if mode == "all" and not report.get("outdated_checked"):
        raise projects.ProjectError("The newest dependency scan did not look up newer releases. Scan again with 'Also look up newer releases' ticked, or choose security fixes only.")
    started = util.now_zulu()
    progress(state="plan", label="Planning the update")
    plan = build_plan(project, report, mode, depversions)
    if not plan["changes"]:
        raise projects.ProjectError("There is nothing to update in the newest dependency scan" + (" for security fixes: no dependency with a known fix is affected." if mode == "security" else "."))
    uid = f"{datetime.datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
    folder = _dir(project["id"]) / uid
    by_file: dict[tuple[str, str], dict[str, str]] = {}
    for c in plan["changes"]:
        for rel in c["files"]:
            by_file.setdefault((c["folder"], rel), {})[_pkg(c["name"]) if c["ecosystem"] == "PyPI" else c["name"]] = c["to"]
    files, skipped, patch = [], [], []
    for (fpath, rel), targets in sorted(by_file.items()):
        progress(state="edit", label=f"Editing a copy of {rel}")
        raw = _read_manifest(fpath, rel)
        label = _label(project, fpath)
        if raw is None:
            skipped.append({"file": _shown(label, rel), "reason": "the file could not be read here (a link, too large, or kept from tools because it looks like credentials)"})
            continue
        try:
            before = raw.decode("utf-8")
        except UnicodeDecodeError:
            skipped.append({"file": _shown(label, rel), "reason": "the file is not UTF-8 text"})
            continue
        after, applied, left = edit_manifest(pathlib.PurePosixPath(rel).name, before, targets)
        for s in left:
            skipped.append({"file": _shown(label, rel), "reason": f"{s['name']}: {s['reason']}"})
        if not applied or after == before:
            continue
        shown = _shown(label, rel)
        for sub, text in (("original", before), ("copy", after)):
            dest = folder / sub / pathlib.PurePosixPath(shown)
            dest.parent.mkdir(parents=True, exist_ok=True)
            fsio.write_bytes(dest, text.encode("utf-8"))
        diff = util.udiff(before, after, f"a/{shown}", f"b/{shown}")
        patch.append(diff)
        files.append({"file": shown, "folder": fpath, "rel": rel, "label": label, "sha256_before": hashlib.sha256(raw).hexdigest(), "sha256_after": hashlib.sha256(after.encode("utf-8")).hexdigest(),
                      "edits": applied, "diff": diff})
    if not files:
        raise projects.ProjectError("None of the manifests could be edited: " + "; ".join(f"{s['file']} ({s['reason']})" for s in skipped[:3]))
    done = {(a["name"]) for f in files for a in f["edits"]}
    changes = [c for c in plan["changes"] if _pkg(c["name"]) in done or c["name"] in done]
    for c in plan["changes"]:
        if c not in changes:
            plan["not_updated"].append({"ecosystem": c["ecosystem"], "name": c["name"], "version": c["from"], "reason": "no manifest of the project could be edited for it"})
    plan["changes"] = changes
    locks = sorted({_shown(_label(project, c["folder"]), l) for c in changes for l in c["locks"]})
    progress(state="predict", label="Predicting the scan after the update")
    predicted = predict(report, changes, depversions, advisories)
    fsio.write_text(folder / "patch.diff", "".join(patch))
    update = {"schema": SCHEMA, "id": uid, "project_id": project["id"], "created": util.now_zulu(), "mode": mode, "mode_words": MODES[mode], "from_scan": report["scan_id"], "status": "candidate",
              "plan": plan, "files": files, "skipped": skipped, "locks_to_regenerate": locks, "predicted": predicted,
              "words": ("This is a candidate for you to review, made on a copy of the manifests. Nothing in the project has changed. No package was installed and no test was run, so it does not "
                        "say that the project still works with the new versions; read the changes, apply them only if you agree, and run your own tests.")}
    fsio.write_json(folder / "update.json", update, indent=2)
    projects.audit(project["id"], tool="update plan", summary=f"{len(changes)} dependencies in {len(files)} files ({MODES[mode].lower()}); a candidate, nothing in the project changed", target="manifests",
                   started=started, ended=util.now_zulu(), exit_status=0)
    return update


def predict(report: dict, changes: list[dict], depversions, advisories) -> dict:
    """What the dependency scan would say about the changed packages at their new versions, by advisory matching against the advisories on this computer. Not an install: only the
    named versions are matched."""
    by_eco: dict[str, list[dict]] = {}
    for c in changes:
        by_eco.setdefault(c["ecosystem"], []).append(c)
    rows = {(r["ecosystem"], r["name"]): r for r in report["dependencies"]}
    out, fixed_ids, remaining_ids, introduced_ids = [], set(), set(), set()
    for eco, items in sorted(by_eco.items()):
        found, source = advisories(eco, [c["name"] for c in items])
        for c in items:
            old = rows[(eco, c["name"])]
            dep = {k: old[k] for k in ("ecosystem", "name", "declared", "source", "manifests", "folder") if k in old}
            now = project_deps.classify({**dep, "version": c["to"]}, found, source, depversions)
            before = {a["id"] for a in old.get("advisories") or []}
            after = {a["id"] for a in now.get("advisories") or []}
            fixed_ids |= before - after
            remaining_ids |= before & after
            introduced_ids |= after - before
            out.append({"ecosystem": eco, "name": c["name"], "from": c["from"], "to": c["to"], "state": now["state"], "fixed": sorted(before - after), "still_affected_by": sorted(before & after),
                        "newly_affected_by": sorted(after - before), "reason": now["reason"]})
    return {"packages": out, "advisories_fixed": sorted(fixed_ids), "advisories_remaining": sorted(remaining_ids), "advisories_introduced": sorted(introduced_ids),
            "words": "By advisory matching against the advisories on this computer, for the changed packages only. It says nothing about the packages that were not changed, about advisories "
                     "not on this computer, or about whether the project works with the new versions."}


# -- reading, applying, undoing ----------------------------------------------------------------------------------------------------------------------------------

def get(project_id: str, uid: str) -> dict:
    if not _ID.fullmatch(uid or ""):
        raise projects.NotFound("There is no such update.")
    try:
        return json.loads((_dir(project_id) / uid / "update.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise projects.NotFound("There is no such update.") from None


def listing(project_id: str) -> list[dict]:
    projects.get(project_id)
    root = _dir(project_id)
    out = []
    for d in sorted(root.iterdir(), reverse=True) if root.is_dir() else []:
        try:
            u = json.loads((d / "update.json").read_text(encoding="utf-8"))
            out.append({"id": u["id"], "created": u["created"], "mode": u["mode"], "mode_words": u["mode_words"], "status": u["status"], "changes": len(u["plan"]["changes"]), "files": len(u["files"]),
                        "applied_at": u.get("applied_at")})
        except (OSError, ValueError, KeyError):
            continue
    return out


def _clean(u: dict) -> dict:
    """An update record whose file names are all plain relative paths; one that is not (a record edited by hand) is refused before any path is built from it."""
    for f in u.get("files", []):
        for name in (f.get("file", ""), f.get("rel", "")):
            parts = pathlib.PurePosixPath(str(name).replace("\\", "/")).parts
            if not name or ".." in parts or pathlib.PurePosixPath(str(name)).is_absolute() or re.match(r"^[A-Za-z]:", str(name)):
                raise projects.ProjectError("This update record names a file outside the project, so it is not used. Make a new plan.")
    return u


def _target(f: dict) -> str:
    root = os.path.realpath(f["folder"])
    return os.path.join(root, *f["rel"].split("/"))


def _check(f: dict, expect: str) -> str | None:
    """Why the real file cannot be written (or undone) now, or None: it must still be the file the plan was made from."""
    raw = _read_manifest(f["folder"], f["rel"])
    if raw is None:
        return f"{f['file']} cannot be read or written here any more"
    if hashlib.sha256(raw).hexdigest() != f[expect]:
        return f"{f['file']} has changed since the update was planned"
    return None


def apply(project: dict, uid: str, confirmed: list[str]) -> dict:
    """Write the candidate's manifests into the project's folders. ``confirmed`` must be exactly the list of files the plan names (the page shows it to the person first). All files are
    checked before any is written; the originals are kept; a write that fails puts back the files already written."""
    projects.require_authorized(project)
    u = _clean(get(project["id"], uid))
    if u["status"] != "candidate":
        raise projects.ProjectError("This update has already been applied or undone; make a new plan.")
    if sorted(confirmed or []) != sorted(f["file"] for f in u["files"]):
        raise projects.ProjectError("The files you confirmed are not the files in the plan. Read the list of files again and confirm it.")
    problems = [p for p in (_check(f, "sha256_before") for f in u["files"]) if p]
    if problems:
        raise projects.ProjectError("Nothing was written. " + "; ".join(problems) + ". Make a new plan from a new dependency scan.")
    folder = _dir(project["id"]) / uid
    started = util.now_zulu()
    written = []
    try:
        for f in u["files"]:
            new = (folder / "copy" / pathlib.PurePosixPath(f["file"])).read_bytes()
            old = _read_manifest(f["folder"], f["rel"])
            if old is None:
                raise OSError("the file changed while it was being written")
            backup = folder / "backup" / pathlib.PurePosixPath(f["file"])
            backup.parent.mkdir(parents=True, exist_ok=True)
            fsio.write_bytes(backup, old)
            fsio.write_bytes(_target(f), new)
            written.append((f, old))
    except OSError as e:
        for f, old in written:
            fsio.write_bytes(_target(f), old)
        raise projects.ProjectError(f"A file could not be written ({e.strerror or type(e).__name__}); the files already written were put back and nothing changed.") from None
    u["status"], u["applied_at"] = "applied", util.now_zulu()
    fsio.write_json(folder / "update.json", u, indent=2)
    projects.audit(project["id"], tool="update apply", summary=f"wrote {len(u['files'])} manifests: {', '.join(f['file'] for f in u['files'])}", target=", ".join(f["folder"] for f in u["files"]),
                   started=started, ended=util.now_zulu(), exit_status=0)
    return u


def revert(project: dict, uid: str) -> dict:
    """Put the originals back, while the files are still exactly as the update wrote them."""
    projects.require_authorized(project)
    u = _clean(get(project["id"], uid))
    if u["status"] != "applied":
        raise projects.ProjectError("Only an applied update can be undone.")
    problems = [p for p in (_check(f, "sha256_after") for f in u["files"]) if p]
    if problems:
        raise projects.ProjectError("Nothing was changed. " + "; ".join(problems) + ", so undoing it here could lose your edits. Restore the originals from the update's backup folder by hand if you want them.")
    folder = _dir(project["id"]) / uid
    started = util.now_zulu()
    for f in u["files"]:
        fsio.write_bytes(_target(f), (folder / "backup" / pathlib.PurePosixPath(f["file"])).read_bytes())
    u["status"], u["reverted_at"] = "reverted", util.now_zulu()
    fsio.write_json(folder / "update.json", u, indent=2)
    projects.audit(project["id"], tool="update undo", summary=f"put back {len(u['files'])} manifests", target=", ".join(f["folder"] for f in u["files"]), started=started, ended=util.now_zulu(), exit_status=0)
    return u


def discard(project_id: str, uid: str) -> None:
    """Delete a candidate (or an undone or applied update's record) from the app. An applied update's backup goes with it, so only a candidate or an undone one can be discarded."""
    u = get(project_id, uid)
    if u["status"] == "applied":
        raise projects.ProjectError("An applied update keeps its backup. Undo it first, or leave the record.")
    shutil.rmtree(_dir(project_id) / uid)
