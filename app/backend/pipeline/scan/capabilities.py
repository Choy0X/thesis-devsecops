"""What the app can do for each package ecosystem, as a table the interface reads at run time: a cell is ``implemented``, ``partial`` or ``not_implemented``, with a sentence.

The page must never show an empty result that looks like "no findings" for something the app cannot check. So a project's ecosystems are looked up here first: a capability
that is not implemented is shown as "not supported yet for this ecosystem". The table is code, next to the code it describes; when a capability is built its cell changes in the
same commit. The advisory data is the one cell that depends on the computer: it is ``implemented`` only when the advisories of that ecosystem are on this computer.
"""
from __future__ import annotations

import pathlib

from app.backend import paths

ECOSYSTEMS = ("PyPI", "npm", "Maven", "Go", "crates.io", "RubyGems", "Packagist", "NuGet", "Hex")
CAPABILITIES = [
    ("manifest_parsing", "Reads the manifests and lock files"),
    ("resolution", "Knows the exact version in use (a lock file) and not only the allowed range"),
    ("advisory_match", "Matches the versions against known advisories"),
    ("outdated_lookup", "Asks the registry for newer releases"),
    ("static_rules", "Static rules over the source code"),
    ("test_execution", "Runs the project's own tests in the sandbox"),
    ("dynamic_probing", "Starts the project in the sandbox and probes it on loopback"),
]
IMPLEMENTED, PARTIAL, NOT, NEEDS_DATA = "implemented", "partial", "not_implemented", "needs_data"

# (status, note) by ecosystem and capability, for what is built. Anything not listed is not implemented.
_CELLS: dict[str, dict[str, tuple[str, str]]] = {
    "PyPI": {
        "manifest_parsing": (PARTIAL, "requirements files, poetry.lock and pyproject.toml; Pipfile.lock and constraints files are not read yet"),
        "resolution": (PARTIAL, "exact only where a file pins the version (==, or a lock file)"),
        "outdated_lookup": (IMPLEMENTED, "PyPI"),
        "static_rules": (PARTIAL, "pattern rules for Python; they find candidates, not proof"),
    },
    "npm": {
        "manifest_parsing": (PARTIAL, "package.json, package-lock.json, yarn.lock and pnpm-lock.yaml"),
        "resolution": (PARTIAL, "exact from a lock file; package.json alone gives only a range"),
        "outdated_lookup": (IMPLEMENTED, "the npm registry"),
        "static_rules": (PARTIAL, "pattern rules for JavaScript and TypeScript; they find candidates, not proof"),
    },
    "Maven": {
        "manifest_parsing": (PARTIAL, "pom.xml and gradle.lockfile; build.gradle and a parent POM are not read"),
        "resolution": (PARTIAL, "exact from gradle.lockfile; a pom gives a version only when it writes one or a property or dependencyManagement entry of the same file does"),
        "outdated_lookup": (IMPLEMENTED, "Maven Central"),
    },
    "Go": {
        "manifest_parsing": (PARTIAL, "go.mod; go.sum is hashes only and is not read"),
        "resolution": (PARTIAL, "go.mod gives the minimum version of each module; the build may use a higher one that another module asks for"),
        "outdated_lookup": (IMPLEMENTED, "the Go module proxy"),
    },
    "crates.io": {
        "manifest_parsing": (PARTIAL, "Cargo.lock (registry crates) and Cargo.toml; git and path dependencies are not registry packages"),
        "resolution": (PARTIAL, "exact from Cargo.lock; in Cargo.toml a bare version is a caret range"),
        "outdated_lookup": (IMPLEMENTED, "crates.io, one request a second"),
    },
    "RubyGems": {
        "manifest_parsing": (PARTIAL, "Gemfile.lock and Gemfile; .gemspec files are not read"),
        "resolution": (PARTIAL, "exact from Gemfile.lock"),
        "outdated_lookup": (IMPLEMENTED, "RubyGems"),
    },
    "Packagist": {
        "manifest_parsing": (PARTIAL, "composer.lock and composer.json"),
        "resolution": (PARTIAL, "exact from composer.lock; dev branches have no version"),
        "outdated_lookup": (IMPLEMENTED, "Packagist"),
    },
    "NuGet": {
        "manifest_parsing": (PARTIAL, "packages.lock.json, packages.config, project files (.csproj, .fsproj, .vbproj) and Directory.Packages.props"),
        "resolution": (PARTIAL, "exact from the lock file or packages.config; a project file gives a version only when it writes a plain one"),
        "outdated_lookup": (IMPLEMENTED, "NuGet"),
    },
    "Hex": {
        "manifest_parsing": (PARTIAL, "mix.lock and mix.exs; git and path dependencies are not Hex packages"),
        "resolution": (PARTIAL, "exact from mix.lock; in mix.exs a plain version is exact and a ~> or >= is a range"),
        "outdated_lookup": (IMPLEMENTED, "hex.pm, at most 100 requests a minute"),
    },
}
_ADVISORY_NOTE = {
    "implemented": "the advisories of this ecosystem are on this computer",
    "missing": "the advisories of this ecosystem have not been downloaded: Download and ingest them under CVE Collections",
}


def advisory_file(ecosystem: str) -> pathlib.Path:
    """The parsed advisories of one ecosystem, as the ingest writes them."""
    from app.backend.pipeline.ingest import osv
    return paths.INTERIM / f"osv_{osv._slug(ecosystem)}.parquet"


def matrix(ecosystems=ECOSYSTEMS) -> dict:
    """``{capabilities, ecosystems: {name: {capability: {status, note}}}}`` for the given ecosystems. The advisory cell is decided by what is on this computer: ``needs_data``
    when the manifests can be read but the advisories of the ecosystem have not been downloaded."""
    out = {}
    for eco in ecosystems:
        cells = {}
        for key, _ in CAPABILITIES:
            status, note = _CELLS.get(eco, {}).get(key, (NOT, ""))
            cells[key] = {"status": status, "note": note}
        has_data = advisory_file(eco).exists()
        if cells["manifest_parsing"]["status"] == NOT:
            cells["advisory_match"] = {"status": NOT, "note": "no manifest can be read for this ecosystem yet" + ("" if has_data else "; its advisories are not on this computer either")}
        elif has_data:
            cells["advisory_match"] = {"status": IMPLEMENTED, "note": _ADVISORY_NOTE["implemented"]}
        else:
            cells["advisory_match"] = {"status": NEEDS_DATA, "note": _ADVISORY_NOTE["missing"]}
        _toolkit_cells(eco, cells)
        out[eco] = cells
    return {"capabilities": [{"key": k, "label": label} for k, label in CAPABILITIES], "ecosystems": out}


def _toolkit_cells(eco: str, cells: dict) -> None:
    """What the sandbox toolkit adds for Python and npm, which depends on whether its image has been built on this computer."""
    if eco not in ("PyPI", "npm"):
        return
    from app.backend.pipeline.scan import sandbox_run
    try:
        sandbox_run.toolkit_image()
        built = True
    except sandbox_run.ToolkitMissing:
        built = False
    no_image = "needs the tool image, which has not been built on this computer yet"
    if not built:
        cells["test_execution"] = {"status": NOT, "note": no_image}
        cells["dynamic_probing"] = {"status": NOT, "note": no_image}
        return
    cells["static_rules"] = {"status": PARTIAL, "note": "pattern rules, and semgrep" + (" and bandit" if eco == "PyPI" else " and eslint with the security plugin") + " in the sandbox; they find candidates, not proof"}
    cells["test_execution"] = ({"status": PARTIAL, "note": "pytest in the sandbox copy; the project's own packages are not installed there, so tests that need them end as an environment error"} if eco == "PyPI"
                               else {"status": NOT, "note": "npm tests need the project's packages, which are not in the copy; they wait for the pre-fetched package cache"})
    cells["dynamic_probing"] = {"status": PARTIAL, "note": "headers, error pages, path traversal and cross-origin answers on the container's own loopback, when the project has a start command and a port"}


def supports(ecosystem: str, capability: str, table: dict | None = None) -> bool:
    cell = ((table or matrix([ecosystem]))["ecosystems"].get(ecosystem) or {}).get(capability)
    return bool(cell and cell["status"] in (IMPLEMENTED, PARTIAL))
