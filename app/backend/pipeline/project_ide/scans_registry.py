"""Version listings from known public registries, with no project execution."""
from __future__ import annotations

import re
from urllib.parse import quote

from app.backend.pipeline.ecosystems import versions as ordering
from app.backend.pipeline.scan.manifests_more import _safe_xml


HOSTS = frozenset({
    "pypi.org", "registry.npmjs.org", "repo.maven.apache.org", "proxy.golang.org",
    "crates.io", "rubygems.org", "repo.packagist.org", "api.nuget.org", "hex.pm",
})


def _go_escape(value):
    return "".join(f"!{char.lower()}" if char.isupper() else char for char in value)


def registry_url(eco, package):
    name = quote(package, safe="")
    if eco == "Maven":
        group, artifact = package.split(":", 1)
        coordinates = "/".join(quote(part, safe="") for part in group.split("."))
        return f"https://repo.maven.apache.org/maven2/{coordinates}/{quote(artifact, safe='')}/maven-metadata.xml"
    if eco == "Go":
        # The module proxy escapes uppercase letters using an exclamation mark.
        return f"https://proxy.golang.org/{quote(_go_escape(package), safe='/!')}/@v/list"
    urls = {
        "PyPI": f"https://pypi.org/pypi/{name}/json",
        "npm": f"https://registry.npmjs.org/{name}",
        "crates.io": f"https://crates.io/api/v1/crates/{name}/versions",
        "RubyGems": f"https://rubygems.org/api/v1/versions/{name}.json",
        "Packagist": f"https://repo.packagist.org/p2/{quote(package, safe='/')}.json",
        "NuGet": f"https://api.nuget.org/v3-flatcontainer/{name.lower()}/index.json",
        "Hex": f"https://hex.pm/api/packages/{name}",
    }
    return urls.get(eco)


def _objects(value):
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError("Registry version records are not objects.")
    return value


def go_retracted(module, releases, fetch_manifest):
    valid = [v for v in releases if v.startswith("v") and ordering.key("Go", v).valid]
    if not valid:
        return []
    stable = [v for v in valid if not ordering.is_prerelease("Go", v)]
    latest = max(stable or valid, key=lambda v: ordering.key("Go", v))
    base = registry_url("Go", module).removesuffix("/list")
    text = fetch_manifest(f"{base}/{quote(_go_escape(latest), safe='!')}.mod").decode("utf-8")
    lines = [line.split("//", 1)[0].strip() for line in text.splitlines()]
    identity = next((line.split(maxsplit=1)[1].strip('"') for line in lines if line.startswith(("module ", "module\t"))), None)
    if identity != module:
        raise ValueError("Go module metadata identity does not match the requested module.")
    block, bounds = False, []
    for line in lines:
        if not line:
            continue
        if block and line == ")":
            block = False
            continue
        if not block:
            if not line.startswith(("retract ", "retract\t")):
                continue
            line = line.split(maxsplit=1)[1]
            if line == "(":
                block = True
                continue
        value = re.fullmatch(r"(v[^\s,\]]+)|\[\s*(v[^\s,\]]+)\s*,\s*(v[^\s,\]]+)\s*\]", line)
        if not value:
            raise ValueError("Go module has an unrecognized retraction.")
        single, lower, upper = value.groups()
        lower, upper = (single, single) if single else (lower, upper)
        if not all(ordering.key("Go", bound).valid for bound in (lower, upper)):
            raise ValueError("Go module has an invalid retraction version.")
        bounds.append((lower, upper))
    if block:
        raise ValueError("Go module retraction block is incomplete.")
    return [version for version in valid if any(ordering.compare("Go", lower, version) <= 0 <=
                                                ordering.compare("Go", upper, version) for lower, upper in bounds)]


def published_versions(eco, package, data):
    if eco == "Maven":
        root = _safe_xml(data.decode("utf-8"), "Maven Central metadata")
        versions = root.find("./versioning/versions")
        if root.tag != "metadata" or versions is None:
            raise ValueError("Maven Central version listing is missing.")
        return [node.text.strip() for node in versions.findall("./version") if node.text]
    if eco == "Go":
        return data.decode("utf-8").splitlines()
    if eco == "PyPI":
        return [v for v, files in data["releases"].items() if any(not f.get("yanked") for f in _objects(files))]
    if eco == "npm":
        return [v for v, item in data["versions"].items() if isinstance(item, dict)]
    if eco == "NuGet":
        return data["versions"]
    if eco == "crates.io":
        return [item["num"] for item in _objects(data["versions"]) if item.get("yanked") is False]
    if eco == "RubyGems":
        return [item["number"] for item in _objects(data) if not item.get("prerelease")]
    if eco == "Packagist":
        return [item["version"] for item in _objects(data["packages"][package]) if "version" in item]
    return [item["version"] for item in _objects(data["releases"]) if not item.get("retirement")]
