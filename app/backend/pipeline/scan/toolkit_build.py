"""Building the tool image of Scan Codebase from a lock file (P3f.4.1): ``resolve`` finds and records exact versions and hashes (it uses the network once), ``build`` makes the image from
the committed Dockerfile and that lock (it uses the network once more, for the base images and the packages), ``check`` runs every tool of the image once in a closed container.

    python -m app.backend.pipeline.scan.toolkit_build resolve
    python -m app.backend.pipeline.scan.toolkit_build rules     # only the Semgrep rules of the commit the lock names, into the build context (a fresh checkout has none; resolve is not needed)
    python -m app.backend.pipeline.scan.toolkit_build build
    python -m app.backend.pipeline.scan.toolkit_build check

What is pinned, and how:

* the three base images (Python, Node, gitleaks) by the digest the registry gave when they were pulled;
* the Python packages (``semgrep``, ``bandit``, ``pytest``, ``coverage`` and everything they need) as wheels for Linux x86-64 and the image's Python, with the SHA-256 of each wheel, written as a
  ``requirements.lock`` that ``pip install --require-hashes`` insists on. Wheels only, so nothing is built or run on this computer to resolve them;
* the npm packages (``eslint``, ``eslint-plugin-security``, ``typescript-eslint``, ``typescript``) as an npm lock file with integrity hashes, made with ``--ignore-scripts``, and installed in the
  image with ``npm ci --ignore-scripts``;
* the Semgrep rules of the public rules repository (Python, JavaScript and TypeScript, the YAML files only) at the commit that was cloned, with the licence text beside them;
* the licence each package says it has (from its metadata), checked against the list the owner approved; a difference is written in the lock and printed, and stops ``build`` until it is accepted.

The lock file (``toolkit/toolkit.lock.json``), ``requirements.lock``, ``js/package.json`` and ``js/package-lock.json`` are kept in the repository. The vendored rules and the assembled build context are
generated under ``data/sandbox/toolkit_context`` and are not committed (their licences differ from this project's).
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

from app.backend import sandbox_runtime, sandbox
from app.backend.pipeline import fsio, util
from app.backend.pipeline.scan import sandbox_run, update_trial

TOOLKIT_DIR = pathlib.Path(__file__).resolve().parent / "toolkit"
LOCK = TOOLKIT_DIR / "toolkit.lock.json"
CONTEXT = pathlib.Path("data/sandbox/toolkit_context")
TAG = "aivdf/scan-tools:1"
BASES = {"python": "python:3.11-slim", "node": "node:20-slim", "gitleaks": "ghcr.io/gitleaks/gitleaks:latest"}
# Two sets of Python packages, so that the packages of the scan tools are not on the path of the project's own tests (semgrep brings requests, rich, jsonschema and more: a project that forgot to list
# one of them would pass its tests by borrowing the image's copy). The scan tools live in a virtual environment; only pytest and coverage are in the Python the tests run with.
PY_VENV = ["semgrep", "bandit", "setuptools<81"]           # setuptools: semgrep imports pkg_resources, which python:3.11-slim does not have (found by the first real scan)
PY_TEST = ["pytest", "coverage"]
PY_SETS = {"venv": PY_VENV, "test": PY_TEST}
NPM_TOOLS = ["eslint", "eslint-plugin-security", "typescript-eslint", "typescript"]
RULES_REPO = "https://github.com/semgrep/semgrep-rules"
RULE_DIRS = ("python", "javascript", "typescript")
# the licences the owner approved the tools under; a package that says something else is flagged
APPROVED = {"semgrep": ["LGPL-2.1"], "bandit": ["Apache-2.0"], "pytest": ["MIT"], "coverage": ["Apache-2.0"], "eslint": ["MIT"], "eslint-plugin-security": ["Apache-2.0", "MIT"],
            "typescript-eslint": ["MIT"], "typescript": ["Apache-2.0"]}
ESLINT_CONFIG = '''// the rules of Scan Codebase's eslint run: the security plugin's recommended set for JavaScript, and the same for TypeScript through its parser
import security from "eslint-plugin-security";
import tseslint from "typescript-eslint";

export default [
  security.configs.recommended,
  { files: ["**/*.ts", "**/*.tsx", "**/*.mts", "**/*.cts"], languageOptions: { parser: tseslint.parser } },
  { ignores: ["**/node_modules/**", "**/dist/**", "**/build/**", "**/.venv/**"] },
];
'''


class ToolkitError(RuntimeError):
    """Why the toolkit could not be resolved, built or checked, in a sentence."""


def sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# -- licences ----------------------------------------------------------------------------------------------------------------------------------------------

def normalise_licence(text: str) -> str:
    """A licence string as short SPDX-like text: ``Apache Software License`` and ``Apache-2.0`` are the same, ``GNU Lesser General Public License v2 (LGPLv2)`` is ``LGPL-2.1`` only if it says 2.1."""
    t = (text or "").strip()
    low = t.lower()
    if "apache" in low:
        return "Apache-2.0"
    if "lgpl" in low or "lesser general public" in low:
        return "LGPL-2.1" if "2.1" in low or "v2" in low or "2.0" in low else "LGPL"
    if low in ("mit", "mit license") or low.startswith("mit ") or "mit license" in low:
        return "MIT"
    if "bsd" in low:
        return "BSD"
    if "isc" in low:
        return "ISC"
    if "python software foundation" in low or low == "psf":
        return "PSF"
    return t[:60] or "unknown"


def wheel_licence(path: pathlib.Path) -> str:
    """The licence a wheel says it has, from its METADATA (the License-Expression or License field, else the classifier)."""
    with zipfile.ZipFile(path) as z:
        meta = next((n for n in z.namelist() if n.endswith(".dist-info/METADATA")), None)
        text = z.read(meta).decode("utf-8", "replace") if meta else ""
    head = text.split("\n\n", 1)[0]
    for field in ("License-Expression", "License"):
        m = re.search(rf"^{field}: (.+)$", head, re.M)
        if m and m.group(1).strip() and m.group(1).strip().upper() != "UNKNOWN" and len(m.group(1)) < 120:
            return normalise_licence(m.group(1))
    m = re.search(r"^Classifier: License :: (?:OSI Approved :: )?(.+)$", head, re.M)
    return normalise_licence(m.group(1)) if m else "unknown"


def wheel_name_version(filename: str) -> tuple[str, str]:
    m = re.match(r"^([A-Za-z0-9_.]+)-([0-9][^-]*)-", filename)
    if not m:
        raise ToolkitError(f"{filename} is not a wheel file name")
    return re.sub(r"[-_.]+", "-", m.group(1)).lower(), m.group(2)


def requirements_lock(wheels: list[dict]) -> str:
    """A ``requirements.lock`` for ``pip install --require-hashes``: one pinned line per wheel with its SHA-256."""
    lines = ["# made by toolkit_build resolve: wheels for Linux x86-64 and the image's Python, each with its hash; installed with pip install --require-hashes"]
    for w in sorted(wheels, key=lambda w: w["name"]):
        lines.append(f"{w['name']}=={w['version']} --hash=sha256:{w['sha256']}")
    return "\n".join(lines) + "\n"


def flags(packages: list[dict]) -> list[dict]:
    """The top-level tools whose licence differs from the one the owner approved."""
    out = []
    for p in packages:
        want = APPROVED.get(p["name"])
        if want and p["licence"] not in want:
            out.append({"name": p["name"], "version": p["version"], "says": p["licence"], "approved": want})
    return out


# -- resolving ---------------------------------------------------------------------------------------------------------------------------------------------

def resolve_bases(client) -> dict:
    """Pull each base image and record the digest the registry gave, with the version of the tool inside where there is one."""
    out = {}
    for key, ref in BASES.items():
        image = client.images.pull(ref)
        image = image[0] if isinstance(image, list) else image
        repo = ref.rsplit(":", 1)[0]
        digests = [d for d in image.attrs.get("RepoDigests", []) if d.startswith(repo + "@")] or image.attrs.get("RepoDigests", [])
        if not digests:
            raise ToolkitError(f"{ref} has no registry digest to pin (it was not pulled from a registry)")
        out[key] = {"ref": ref, "pinned": digests[0], "id": image.id, "size_mb": round(image.attrs.get("Size", 0) / 1e6)}
    return out


def resolve_python(work: pathlib.Path, runner=subprocess.run) -> dict:
    """Download the wheels of each set of Python tools and everything it needs, for the image's platform, and describe them (name, version, SHA-256, licence): ``{set: {wheels, requirements_lock,
    packages}}``."""
    out = {}
    for key, tools in PY_SETS.items():
        dest = work / "wheels" / key
        got = update_trial.prefetch(tools, dest, runner=runner)
        if not got["ok"]:
            raise ToolkitError(f"The Python tools ({', '.join(tools)}) could not be resolved as wheels: " + got["note"])
        wheels = []
        for f in sorted(dest.glob("*.whl")):
            name, version = wheel_name_version(f.name)
            wheels.append({"name": name, "version": version, "file": f.name, "sha256": sha256_file(f), "licence": wheel_licence(f), "bytes": f.stat().st_size})
        out[key] = {"wheels": wheels, "requirements_lock": requirements_lock(wheels), "packages": [w for w in wheels if w["name"] in APPROVED]}
    return out


def resolve_npm(work: pathlib.Path, runner=subprocess.run) -> dict:
    """Make ``package.json`` (exact versions) and ``package-lock.json`` (integrity hashes) for the npm tools, without running any package script."""
    npm = shutil.which("npm")
    if not npm:
        raise ToolkitError("npm is not installed on this computer, so the eslint packages cannot be resolved")
    folder = work / "js"
    folder.mkdir(parents=True, exist_ok=True)
    fsio.write_text(folder / "package.json", json.dumps({"name": "aivdf-scan-tools-js", "private": True, "version": "0.0.0", "type": "module"}, indent=2) + "\n")
    done = runner([npm, "install", "--package-lock-only", "--ignore-scripts", "--save-exact", "--no-audit", "--no-fund", *NPM_TOOLS], cwd=folder, capture_output=True, text=True, timeout=600)
    if done.returncode != 0:
        raise ToolkitError("npm could not resolve the packages: " + (done.stderr or done.stdout).strip().splitlines()[-1][:300])
    pkg = json.loads((folder / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((folder / "package-lock.json").read_text(encoding="utf-8"))
    packages = []
    for name in NPM_TOOLS:
        entry = (lock.get("packages") or {}).get(f"node_modules/{name}") or {}
        packages.append({"name": name, "version": entry.get("version") or pkg["dependencies"].get(name), "licence": normalise_licence(str(entry.get("license") or "")), "integrity": entry.get("integrity")})
    fsio.write_text(folder / "eslint.config.mjs", ESLINT_CONFIG)
    return {"dir": folder, "packages": packages, "count": len(lock.get("packages") or {}) - 1, "package_lock_sha256": sha256_file(folder / "package-lock.json")}


def resolve_rules(work: pathlib.Path, runner=subprocess.run) -> dict:
    """Clone the public Semgrep rules (a shallow clone), keep the YAML files of the languages in use and the licence, and record the commit."""
    git = shutil.which("git")
    if not git:
        raise ToolkitError("git is not installed on this computer, so the Semgrep rules cannot be fetched")
    repo = work / "semgrep-rules"
    done = runner([git, "clone", "--depth", "1", "--quiet", RULES_REPO, str(repo)], capture_output=True, text=True, timeout=600)
    if done.returncode != 0:
        raise ToolkitError("The Semgrep rules could not be cloned: " + (done.stderr or "").strip().splitlines()[-1][:300])
    commit = runner([git, "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    out = work / "rules"
    count = 0
    for d in RULE_DIRS:
        for f in (repo / d).rglob("*.yaml"):
            dest = out / f.relative_to(repo)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, dest)
            count += 1
    for name in ("LICENSE", "LICENSE.md", "LICENSE.txt"):
        if (repo / name).is_file():
            shutil.copyfile(repo / name, out / name)
    return {"dir": out, "commit": commit, "repo": RULES_REPO, "dirs": list(RULE_DIRS), "files": count, "licence_file": next((n for n in ("LICENSE", "LICENSE.md", "LICENSE.txt") if (out / n).is_file()), None)}


def resolve(client=None, *, runner=subprocess.run, base: pathlib.Path | None = None) -> dict:
    """Resolve everything and write the lock file, ``requirements.lock``, ``js/package.json`` and ``js/package-lock.json`` into ``toolkit/``, and the rules into the build context."""
    client = client or sandbox._client()
    with tempfile.TemporaryDirectory(prefix="toolkit-resolve-") as tmp:
        work = pathlib.Path(tmp)
        bases = resolve_bases(client)
        py = resolve_python(work, runner)
        js = resolve_npm(work, runner)
        rules = resolve_rules(work, runner)
        folder = base or TOOLKIT_DIR
        for key in PY_SETS:
            fsio.write_text(folder / f"requirements.{key}.lock", py[key]["requirements_lock"])
        (folder / "js").mkdir(parents=True, exist_ok=True)
        for name in ("package.json", "package-lock.json", "eslint.config.mjs"):
            shutil.copyfile(js["dir"] / name, folder / "js" / name)
        ctx = CONTEXT / "rules"
        if ctx.exists():
            shutil.rmtree(ctx)
        shutil.copytree(rules["dir"], ctx)
        packages = [p for key in PY_SETS for p in py[key]["packages"]] + js["packages"]
        every = [w for key in PY_SETS for w in py[key]["wheels"]]
        lock = {"schema": "toolkit-lock/1", "resolved_at": util.now_zulu(), "image_tag": TAG, "python": "3.11", "bases": bases,
                "python_packages": {key: {"count": len(py[key]["wheels"]), "requirements_lock_sha256": hashlib.sha256(py[key]["requirements_lock"].encode()).hexdigest(), "tools": py[key]["packages"]}
                                    for key in PY_SETS},
                "npm_packages": {"count": js["count"], "package_lock_sha256": js["package_lock_sha256"], "tools": js["packages"]},
                "rules": {k: v for k, v in rules.items() if k != "dir"}, "licences": {"approved": APPROVED, "flags": flags(packages), "all_python": sorted({(w["name"], w["licence"]) for w in every})}}
        lock["licences"]["all_python"] = [list(x) for x in lock["licences"]["all_python"]]
        fsio.write_json(folder / "toolkit.lock.json", lock, indent=2)
    return lock


def vendor_rules(lock: dict | None = None, context: pathlib.Path | None = None, runner=subprocess.run) -> dict:
    """Put the Semgrep rules of the commit the lock names into the build context (``resolve`` leaves them there, but a fresh checkout has none, and resolving again would change the lock). The
    commit is fetched by its hash, checked out, and its YAML files and licence are copied; a different number of files than the lock recorded is an error (the repository was rewritten)."""
    lock = lock or load_lock()
    wanted = lock["rules"]
    git = shutil.which("git")
    if not git:
        raise ToolkitError("git is not installed on this computer, so the Semgrep rules cannot be fetched")
    ctx = (context or CONTEXT) / "rules"
    with tempfile.TemporaryDirectory(prefix="toolkit-rules-") as tmp:
        repo = pathlib.Path(tmp) / "semgrep-rules"
        repo.mkdir()
        for argv in ([git, "init", "--quiet"], [git, "remote", "add", "origin", wanted["repo"]], [git, "fetch", "--depth", "1", "--quiet", "origin", wanted["commit"]], [git, "checkout", "--quiet", "FETCH_HEAD"]):
            done = runner(argv, cwd=repo, capture_output=True, text=True, timeout=600)
            if done.returncode != 0:
                raise ToolkitError(f"The Semgrep rules of commit {wanted['commit'][:10]} could not be fetched ({argv[1]}): " + ((done.stderr or "").strip().splitlines() or ["no reason given"])[-1][:300])
        head = runner([git, "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
        if head != wanted["commit"]:
            raise ToolkitError(f"The rules came back at commit {head[:10]}, not the {wanted['commit'][:10]} the lock names.")
        if ctx.exists():
            shutil.rmtree(ctx)
        count = 0
        for d in wanted["dirs"]:
            for f in (repo / d).rglob("*.yaml"):
                dest = ctx / f.relative_to(repo)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(f, dest)
                count += 1
        for name in ("LICENSE", "LICENSE.md", "LICENSE.txt"):
            if (repo / name).is_file():
                shutil.copyfile(repo / name, ctx / name)
    if count != wanted["files"]:
        shutil.rmtree(ctx, ignore_errors=True)
        raise ToolkitError(f"The rules at commit {wanted['commit'][:10]} hold {count} files, and the lock recorded {wanted['files']}. Nothing was kept; run resolve to take new ones.")
    return {"commit": wanted["commit"], "files": count, "to": str(ctx)}


# -- building ----------------------------------------------------------------------------------------------------------------------------------------------

def load_lock(path: pathlib.Path | None = None) -> dict:
    try:
        lock = json.loads((path or LOCK).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ToolkitError("There is no toolkit lock file yet: run resolve first.") from None
    if lock.get("schema") != "toolkit-lock/1":
        raise ToolkitError("The toolkit lock file is of an unknown kind.")
    return lock


def build_args(lock: dict) -> dict:
    return {"PYTHON_IMAGE": lock["bases"]["python"]["pinned"], "NODE_IMAGE": lock["bases"]["node"]["pinned"], "GITLEAKS_IMAGE": lock["bases"]["gitleaks"]["pinned"]}


def assemble_context(lock: dict, folder: pathlib.Path | None = None, context: pathlib.Path | None = None) -> pathlib.Path:
    """The build context: the Dockerfile and lock files from ``toolkit/``, the vendored rules (made by resolve), and ``licences.json`` made from the lock."""
    folder, ctx = folder or TOOLKIT_DIR, context or CONTEXT
    if not (ctx / "rules").is_dir():
        raise ToolkitError("The Semgrep rules are not in the build context: run resolve first.")
    for name in ("Dockerfile", *[f"requirements.{key}.lock" for key in PY_SETS]):
        shutil.copyfile(folder / name, ctx / name)
    (ctx / "js").mkdir(parents=True, exist_ok=True)
    for name in ("package.json", "package-lock.json", "eslint.config.mjs"):
        shutil.copyfile(folder / "js" / name, ctx / "js" / name)
    for key in PY_SETS:
        name = f"requirements.{key}.lock"
        if hashlib.sha256((ctx / name).read_bytes()).hexdigest() != lock["python_packages"][key]["requirements_lock_sha256"]:
            raise ToolkitError(f"{name} is not the file the lock was made with: run resolve again.")
    if sha256_file(ctx / "js" / "package-lock.json") != lock["npm_packages"]["package_lock_sha256"]:
        raise ToolkitError("package-lock.json is not the file the lock was made with: run resolve again.")
    fsio.write_json(ctx / "licences.json", {"python": [t for key in PY_SETS for t in lock["python_packages"][key]["tools"]], "npm": lock["npm_packages"]["tools"], "rules": lock["rules"], "approved": lock["licences"]["approved"]}, indent=2)
    return ctx


def build(client=None, *, accept_licences: bool = False, on_line=lambda line: None) -> dict:
    """Build the image from the committed Dockerfile and the lock, and record it in ``data/sandbox/toolkit_image.json``."""
    client = client or sandbox._client()
    lock = load_lock()
    if lock["licences"]["flags"] and not accept_licences:
        raise ToolkitError("These packages say a licence other than the approved one: " + "; ".join(f"{f['name']} {f['version']} says {f['says']}, approved {'/'.join(f['approved'])}" for f in lock["licences"]["flags"])
                           + ". Read them, and build again with the licences accepted if they are fine.")
    ctx = assemble_context(lock)
    log = []
    try:
        stream = client.api.build(path=str(ctx), tag=TAG, buildargs=build_args(lock), labels={"aivdf.image": "1", "aivdf.toolkit": "scan-codebase-tier1"}, rm=True, decode=True, pull=False, nocache=False)
        for chunk in stream:
            if chunk.get("error"):
                raise ToolkitError("The build failed: " + str(chunk["error"]).strip()[-600:])
            text = (chunk.get("stream") or "").strip()
            if text:
                log.append(text)
                on_line(text)
    except ToolkitError:
        raise
    except Exception as e:
        raise ToolkitError(f"The build could not run ({type(e).__name__}: {str(e)[:200]})") from None
    image = client.images.get(TAG)
    record = {"tag": TAG, "id": image.id, "built_at": util.now_zulu(), "size_mb": round(image.attrs.get("Size", 0) / 1e6), "lock_resolved_at": lock["resolved_at"],
              "bases": {k: v["pinned"] for k, v in lock["bases"].items()}, "log_tail": log[-15:]}
    fsio.write_json(sandbox_run.TOOLKIT_IMAGE, record, indent=2)
    return record


# -- checking ----------------------------------------------------------------------------------------------------------------------------------------------

CHECKS = [("semgrep", ["semgrep", "--version"]), ("bandit", ["bandit", "--version"]), ("gitleaks", ["gitleaks", "version"]), ("pytest", ["python", "-m", "pytest", "--version"]),
          ("coverage", ["python", "-m", "coverage", "--version"]), ("node", ["node", "--version"]), ("eslint", ["node", "/opt/toolkit/js/node_modules/eslint/bin/eslint.js", "--version"]),
          ("rules", ["sh", "-c", "find /opt/toolkit/rules -name '*.yaml' | wc -l"]), ("user", ["id", "-u"]),
          ("semgrep scan", ["sh", "-c", "mkdir -p /tmp/t && printf 'import os\nos.system(input())\n' > /tmp/t/t.py && semgrep scan --json --quiet --metrics=off --disable-version-check "
                            "--config /opt/toolkit/rules /tmp/t | head -c 300"])]


def check(client=None) -> dict:
    """Run each tool of the image once, in a container with no network and no mount, and return what each said."""
    client = client or sandbox._client()
    image = sandbox_run.toolkit_image()
    ct = sandbox_runtime.run_container(client, **sandbox_run.container_kwargs(image["tag"], f"{sandbox.PREFIX}toolkit-check"))
    out = {}
    try:
        for name, argv in CHECKS:
            res = ct.exec_run(argv, workdir="/work", demux=False)
            out[name] = {"exit_code": res.exit_code, "output": (res.output or b"").decode("utf-8", "replace").strip()[-300:]}
    finally:
        ct.remove(force=True, v=True)
    return out


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else ""
    try:
        if cmd == "resolve":
            lock = resolve()
            print(json.dumps({k: lock[k] for k in ("resolved_at", "bases", "licences")}, indent=2, default=str)[:4000])
            print(f"python packages: {sum(lock['python_packages'][k]['count'] for k in PY_SETS)}, npm packages: {lock['npm_packages']['count']}, rules: {lock['rules']['files']} files at {lock['rules']['commit'][:12]}")
        elif cmd == "rules":
            print(json.dumps(vendor_rules(), indent=2))
        elif cmd == "build":
            rec = build(accept_licences="--accept-licences" in argv, on_line=lambda line: print(line[-200:].encode("ascii", "replace").decode("ascii")))                  # pip draws progress bars with characters the Windows console cannot print
            print(json.dumps(rec, indent=2, default=str, ensure_ascii=True)[:2000])
        elif cmd == "check":
            print(json.dumps(check(), indent=2))
        else:
            print(__doc__)
            return 2
    except ToolkitError as e:
        print(str(e))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
