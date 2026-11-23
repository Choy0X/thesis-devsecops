"""Registry of ecosystem adapters and the path/identity helpers the pipeline calls.

``ADAPTERS`` lists the nine package ecosystems that are mined plus three package-less
OSV sources (GIT, Linux, OSS-Fuzz) whose adapters exist but are not enabled: which are
mined is ``data.ecosystems`` in the phase config. ``package_key`` and the path
classifiers fall back to the global rules for an ecosystem without an adapter.
"""
from __future__ import annotations

import re

from app.backend.pipeline.ecosystems.base import (COMMON_GENERATED, COMMON_TEST, COMMON_VENDOR,
                                                  EcosystemAdapter, repository_identity)

__all__ = ["ADAPTERS", "COMMON_CONFIG", "COMMON_DOCS", "COMMON_GENERATED", "COMMON_TEST", "COMMON_VENDOR",
           "EcosystemAdapter", "categorize_path", "enabled", "get", "is_generated_path", "is_manifest_path",
           "is_test_path", "is_vendored_path", "package_key", "project_key", "repository_identity", "subsystem_of"]


def _lower(n: str) -> str:
    return n.strip().lower()


def _pypi(n: str) -> str:                      # PEP 503 normalisation
    return re.sub(r"[-_.]+", "-", n.strip()).lower()


def _crates(n: str) -> str:                    # crates.io treats - and _ as the same name
    return n.strip().lower().replace("_", "-")


def _go(n: str) -> str:                        # a major-version suffix names the same repository
    n = n.strip().lower()
    n = re.sub(r"/v\d+$", "", n)
    return re.sub(r"\.v\d+$", "", n)


def _none(n: str) -> str:
    return n.strip().lower()


ADAPTERS: list[EcosystemAdapter] = [
    EcosystemAdapter(
        "npm", _lower,
        manifest_files=("package.json", "package-lock.json", "npm-shrinkwrap.json", "yarn.lock",
                        "pnpm-lock.yaml", ".npmrc"),
        test_patterns=(r"\.e2e-spec\.[jt]sx?$", r"\.snapshot\.[jt]sx?$", r"(^|/)__snapshots__/",
                       r"(^|/)(e2e|cypress)/"),
        vendor_patterns=(r"(^|/)(build|out|\.next|coverage|bower_components)/",),
        generated_patterns=(r"\.d\.[cm]?ts$", r"\.snap$")),
    EcosystemAdapter(
        "PyPI", _pypi,
        manifest_files=("requirements*.txt", "pyproject.toml", "setup.py", "setup.cfg", "Pipfile",
                        "Pipfile.lock", "poetry.lock", "tox.ini"),
        test_patterns=(r"(^|/)tests?\.py$", r"_tests?\.py$"),
        vendor_patterns=(r"(^|/)(\.tox|\.venv|venv|build|\.eggs|__pycache__)/", r"(^|/)_vendor/"),
        generated_patterns=(r"_pb2(_grpc)?\.py$",)),
    EcosystemAdapter(
        "Maven", _lower,
        manifest_files=("pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle",
                        "gradle.properties"),
        test_patterns=(r"(^|/)src/(test|it|integration-test)/",
                       r"(?-i:(Test|Tests|IT|ITCase|TestCase)\.(java|kt|scala|groovy)$)"),
        vendor_patterns=(r"(^|/)(target|build|\.gradle|out)/",),
        generated_patterns=(r"(^|/)generated(-sources)?/", r"\.generated\.(java|kt)$")),
    EcosystemAdapter(
        "Go", _go,
        manifest_files=("go.mod", "go.sum"),
        test_patterns=(r"_test\.go$", r"(^|/)testdata/"),
        vendor_patterns=(r"(^|/)vendor/",),
        generated_patterns=(r"\.pb\.go$", r"_gen\.go$", r"\.gen\.go$", r"(^|/)zz_generated\.[^/]+$")),
    EcosystemAdapter(
        "crates.io", _crates,
        manifest_files=("Cargo.toml", "Cargo.lock"),
        test_patterns=(r"(^|/)tests/", r"_tests?\.rs$", r"(^|/)benches/"),
        vendor_patterns=(r"(^|/)(target|vendor)/",),
        generated_patterns=(r"\.pb\.rs$",)),
    EcosystemAdapter(
        "RubyGems", _lower,
        manifest_files=("Gemfile", "Gemfile.lock", "*.gemspec"),
        test_patterns=(r"_spec\.rb$", r"_test\.rb$", r"(^|/)(spec|test|features)/"),
        vendor_patterns=(r"(^|/)(vendor|\.bundle)/",)),
    EcosystemAdapter(
        "Packagist", _lower,
        manifest_files=("composer.json", "composer.lock"),
        test_patterns=(r"(^|/)tests?/", r"(?-i:(Test|Tests|TestCase)\.php$)"),
        vendor_patterns=(r"(^|/)vendor/", r"(^|/)var/cache/"),
        generated_patterns=(r"\.cache\.php$",)),
    EcosystemAdapter(
        "NuGet", _lower,
        manifest_files=("*.csproj", "*.fsproj", "*.vbproj", "packages.config", "Directory.Packages.props",
                        "nuget.config"),
        test_patterns=(r"(^|/)[^/]*\.(Unit|Integration)?Tests?(\.[^/]*)?/", r"Tests?\.cs$"),
        vendor_patterns=(r"(^|/)(bin|obj|packages|Pods)/",),
        generated_patterns=(r"\.Designer\.cs$", r"\.g\.(i\.)?cs$", r"\.generated\.cs$")),
    EcosystemAdapter(
        "Hex", _lower,
        manifest_files=("mix.exs", "mix.lock", "rebar.config"),
        test_patterns=(r"_test\.exs?$", r"(^|/)test/"),
        vendor_patterns=(r"(^|/)(deps|_build)/",)),
    # package-less OSV sources: adapters exist, mining them is not enabled in this phase
    EcosystemAdapter("GIT", _none, manifest_files=("CMakeLists.txt", "Makefile", "configure.ac", "meson.build"),
                     test_patterns=(r"(^|/)(testsuite|tests?)/",), package_less=True,
                     vendor_patterns=(r"(^|/)(third_party|contrib|deps)/",),
                     generated_patterns=(r"\.pb\.(cc|h)$", r"\.generated\.")),
    EcosystemAdapter("Linux", _none, manifest_files=("Makefile", "Kconfig", "Kbuild"),
                     test_patterns=(r"(^|/)tools/testing/", r"(^|/)samples/"), package_less=True,
                     vendor_patterns=(r"(^|/)Documentation/",)),
    EcosystemAdapter("OSS-Fuzz", _none, manifest_files=("CMakeLists.txt", "Makefile", "Dockerfile"),
                     test_patterns=(r"(^|/)(fuzz|tests?)/",), package_less=True,
                     vendor_patterns=(r"(^|/)(third_party|contrib|deps)/",)),
]
_BY_NAME = {a.name.lower(): a for a in ADAPTERS}


def get(name: str | None) -> EcosystemAdapter | None:
    """The adapter of an ecosystem name (case-insensitive); None when there is none."""
    return _BY_NAME.get((name or "").strip().lower())


def enabled() -> list[EcosystemAdapter]:
    """The adapters named by ``data.ecosystems`` in the config, in that order."""
    from app.backend import project_config
    out = []
    for n in project_config.config().data.ecosystems:
        a = get(n)
        if a is None:
            raise ValueError(f"data.ecosystems names an ecosystem without an adapter: {n}")
        out.append(a)
    return out


def package_key(ecosystem: str | None, packages: str | None) -> str | None:
    """'<ecosystem>:<first canonical package>' (PEP 503 for PyPI, module path without a major
    suffix for Go, ...); an ecosystem without an adapter falls back to lower case."""
    a = get(ecosystem)
    if a:
        return a.package_key(packages)
    names = sorted({p.strip().lower() for p in (packages or "").split(",") if p.strip()})
    eco = (ecosystem or "").strip().lower()
    return f"{eco}:{names[0]}" if names else None


def subsystem_of(path: str | None) -> str:
    """The first two directories of a source path ('drivers/net/ethernet/x.c' -> 'drivers/net', 'mm/slub.c' -> 'mm', 'main.c' -> 'root'): the unit a large
    project without packages (the Linux kernel) is split by, so one subsystem never sits in two partitions."""
    parts = [p for p in (path or "").replace("\\", "/").split("/") if p][:-1]
    return "/".join(parts[:2]) if parts else "root"


def project_key(ecosystem: str | None, package: str | None, repo_url: str | None = None, path: str | None = None) -> str | None:
    """The project a unit belongs to, the key partitions and caps group by. A package ecosystem keeps its package; a package-less source (OSV GIT, Linux, OSS-Fuzz)
    has none, so a repository is its project ('<host>/<owner>/<repo>') and the kernel, which is one repository, is split by subsystem ('linux:drivers/net')."""
    if isinstance(package, str) and package.strip():
        return package
    a = get(ecosystem)
    if not (a and a.package_less):
        return package
    if a.name == "Linux":
        return f"linux:{subsystem_of(path)}"
    return repository_identity(repo_url)


COMMON_DOCS = re.compile(
    r"(^|/)(docs?|documentation)(/|$)|\.(md|mdx|rst|adoc|txt)$|"
    r"(^|/)(README|CHANGELOG|CHANGES|HISTORY|LICENSE|NOTICE)(\.[a-z]+)?$", re.I)
COMMON_CONFIG = re.compile(
    r"\.(json|ya?ml|toml|ini|cfg|conf|properties|env|xml|gradle)$|(^|/)(Dockerfile|Makefile|docker-compose[^/]*)$|"
    r"(^|/)\.github/|(^|/)\.(npmrc|babelrc|eslintrc[^/]*|editorconfig)$", re.I)
VENDOR_DIRS = re.compile(r"(^|/)(vendor|vendored|third[_-]party|node_modules|dist|site-packages)/", re.I)


def categorize_path(path: str, ecosystem: str | None = None, language: str | None = None) -> str:
    """What kind of file a changed path is: generated, vendor, docs, manifest, test, config, source
    or other. Configuration and manifest changes are kept as evidence, never dropped by extension."""
    a = get(ecosystem)
    if COMMON_GENERATED.search(path) or (a and a._hit("generated", path)):
        return "generated"
    if VENDOR_DIRS.search(path) or (a and a._hit("vendor", path)):
        return "vendor"
    if COMMON_DOCS.search(path):
        return "docs"
    if a and a.is_manifest_path(path):
        return "manifest"
    if is_test_path(path, ecosystem):
        return "test"
    if COMMON_CONFIG.search(path):
        return "config"
    return "source" if language else "other"


def is_test_path(path: str, ecosystem: str | None = None) -> bool:
    a = get(ecosystem)
    return a.is_test_path(path) if a else bool(COMMON_TEST.search(path))


def is_vendored_path(path: str, ecosystem: str | None = None) -> bool:
    a = get(ecosystem)
    return a.is_vendored_path(path) if a else bool(COMMON_VENDOR.search(path))


def is_generated_path(path: str, ecosystem: str | None = None) -> bool:
    a = get(ecosystem)
    return a.is_generated_path(path) if a else bool(COMMON_GENERATED.search(path))


def is_manifest_path(path: str, ecosystem: str | None = None) -> bool:
    a = get(ecosystem)
    return a.is_manifest_path(path) if a else False
