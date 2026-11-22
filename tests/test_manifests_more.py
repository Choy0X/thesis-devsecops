"""Readers for Go, Maven and Gradle, RubyGems and Packagist manifests (P3f.8.1): what each file says about the exact version in use, and what it leaves unknown."""
import pathlib

import pytest

from app.backend.pipeline.scan import manifests as mf
from app.backend.pipeline.scan import manifests_more as more

GO_MOD = """module example.com/shop

go 1.21

require github.com/pkg/errors v0.9.1

require (
	github.com/gin-gonic/gin v1.9.1
	golang.org/x/text v0.3.7 // indirect
	example.com/old v0.0.0-20210101000000-abcdef123456
)

replace github.com/pkg/errors => ./errors
exclude golang.org/x/net v1.0.0
"""

POM = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.example</groupId><artifactId>shop</artifactId><version>2.1.0</version>
  <properties><spring.version>5.3.20</spring.version><jackson.version>${spring.version}</jackson.version></properties>
  <dependencyManagement><dependencies>
    <dependency><groupId>org.apache.logging.log4j</groupId><artifactId>log4j-core</artifactId><version>2.14.1</version></dependency>
  </dependencies></dependencyManagement>
  <dependencies>
    <dependency><groupId>org.springframework</groupId><artifactId>spring-core</artifactId><version>${spring.version}</version></dependency>
    <dependency><groupId>com.fasterxml.jackson.core</groupId><artifactId>jackson-databind</artifactId><version>${jackson.version}</version></dependency>
    <dependency><groupId>org.apache.logging.log4j</groupId><artifactId>log4j-core</artifactId></dependency>
    <dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>[4.12,5.0)</version><scope>test</scope></dependency>
    <dependency><groupId>com.example</groupId><artifactId>inherited</artifactId></dependency>
    <dependency><groupId>com.example</groupId><artifactId>unresolved</artifactId><version>${not.defined}</version></dependency>
  </dependencies>
</project>
"""

GRADLE_LOCK = """# This is a Gradle generated file for dependency locking.
org.springframework:spring-core:5.3.20=compileClasspath,runtimeClasspath
com.google.guava:guava:31.0.1-jre=runtimeClasspath
empty=annotationProcessor
"""

GEM_LOCK = """GEM
  remote: https://rubygems.org/
  specs:
    actionpack (7.0.4)
      actionview (= 7.0.4)
      rack (~> 2.0, >= 2.2.0)
    nokogiri (1.13.9-x86_64-linux)
    rack (2.2.4)

PLATFORMS
  x86_64-linux

DEPENDENCIES
  actionpack (~> 7.0)
  rack
"""

GEMFILE = """source 'https://rubygems.org'
gem 'rails', '~> 7.0.4'
gem "rack", "2.2.4"
gem 'puma', '>= 5.0', '< 7'
gem 'pinned', '= 1.0.0'
gem 'dev_tool', git: 'https://example.com/tool.git'   # a comment
gem 'bare'
"""

COMPOSER_LOCK = '{"packages": [{"name": "monolog/monolog", "version": "2.8.0"}, {"name": "symfony/console", "version": "v5.4.12"}, {"name": "x/dev", "version": "dev-main"}], "packages-dev": [{"name": "phpunit/phpunit", "version": "9.5.25"}]}'
COMPOSER_JSON = '{"require": {"php": ">=8.0", "ext-json": "*", "monolog/monolog": "^2.0", "guzzlehttp/guzzle": "7.4.5"}, "require-dev": {"phpunit/phpunit": "^9.5"}}'


def read(tmp_path, name, text, reader):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return reader(p, name)


def by_name(deps):
    return {d["name"]: d for d in deps}


def test_go_mod_gives_the_required_modules_with_the_version_without_its_v_and_leaves_out_what_is_not_a_requirement(tmp_path):
    deps = by_name(read(tmp_path, "go.mod", GO_MOD, more.go_mod))
    assert sorted(deps) == ["example.com/old", "github.com/gin-gonic/gin", "github.com/pkg/errors", "golang.org/x/text"]
    gin = deps["github.com/gin-gonic/gin"]
    assert (gin["ecosystem"], gin["version"], gin["declared"], gin["source"], gin["manifest"]) == ("Go", "1.9.1", "v1.9.1", "pinned", "go.mod")
    assert deps["example.com/old"]["version"] == "0.0.0-20210101000000-abcdef123456"


def test_a_pom_resolves_properties_and_managed_versions_and_says_what_it_cannot_know(tmp_path):
    deps = by_name(read(tmp_path, "pom.xml", POM, more.maven_pom))
    assert deps["org.springframework:spring-core"]["version"] == "5.3.20" and deps["org.springframework:spring-core"]["source"] == "pinned"
    assert deps["com.fasterxml.jackson.core:jackson-databind"]["version"] == "5.3.20", "a property that names another property"
    assert deps["org.apache.logging.log4j:log4j-core"]["version"] == "2.14.1", "the version comes from dependencyManagement"
    assert deps["junit:junit"]["version"] is None and deps["junit:junit"]["declared"] == "[4.12,5.0)" and deps["junit:junit"]["source"] == "declared"
    assert deps["com.example:inherited"]["version"] is None and deps["com.example:inherited"]["declared"] is None, "managed by a parent POM, which is not read"
    assert deps["com.example:unresolved"]["version"] is None and deps["com.example:unresolved"]["declared"] == "${not.defined}"
    assert all(d["ecosystem"] == "Maven" for d in deps.values())


@pytest.mark.parametrize("text", ['<!DOCTYPE project [<!ENTITY a "b">]><project/>', "<!doctype x><project/>", "<project>"])
def test_a_pom_with_a_document_type_or_broken_xml_is_refused_with_a_sentence(tmp_path, text):
    with pytest.raises(mf.ManifestError, match="pom.xml"):
        read(tmp_path, "pom.xml", text, more.maven_pom)


def test_a_gradle_lock_file_is_a_list_of_exact_versions(tmp_path):
    deps = by_name(read(tmp_path, "gradle.lockfile", GRADLE_LOCK, more.gradle_lockfile))
    assert sorted(deps) == ["com.google.guava:guava", "org.springframework:spring-core"]
    assert (deps["com.google.guava:guava"]["version"], deps["com.google.guava:guava"]["source"]) == ("31.0.1-jre", "lock")


def test_a_gemfile_lock_lists_the_gems_it_resolved_and_not_the_constraints_under_them(tmp_path):
    deps = by_name(read(tmp_path, "Gemfile.lock", GEM_LOCK, more.gem_lock))
    assert sorted(deps) == ["actionpack", "nokogiri", "rack"]
    assert (deps["rack"]["version"], deps["rack"]["source"]) == ("2.2.4", "lock")
    assert deps["nokogiri"]["version"] == "1.13.9" and deps["nokogiri"]["declared"] == "1.13.9-x86_64-linux", "the platform is not part of the version"


def test_a_gemfile_gives_ranges_and_only_an_exact_one_is_a_version(tmp_path):
    deps = by_name(read(tmp_path, "Gemfile", GEMFILE, more.gemfile))
    assert sorted(deps) == ["bare", "dev_tool", "pinned", "puma", "rack", "rails"]
    assert (deps["rack"]["version"], deps["rack"]["source"]) == ("2.2.4", "pinned")
    assert deps["pinned"]["version"] == "1.0.0"
    assert deps["rails"]["version"] is None and deps["rails"]["declared"] == "~> 7.0.4"
    assert deps["puma"]["version"] is None and deps["puma"]["declared"] == ">= 5.0, < 7"
    assert deps["bare"]["declared"] is None and deps["dev_tool"]["version"] is None


def test_composer_lock_is_exact_and_leaves_out_development_branches(tmp_path):
    deps = by_name(read(tmp_path, "composer.lock", COMPOSER_LOCK, more.composer_lock))
    assert sorted(deps) == ["monolog/monolog", "phpunit/phpunit", "symfony/console"]
    assert (deps["symfony/console"]["version"], deps["symfony/console"]["declared"], deps["symfony/console"]["source"]) == ("5.4.12", "v5.4.12", "lock")


def test_composer_json_gives_constraints_and_skips_the_php_and_extension_requirements(tmp_path):
    deps = by_name(read(tmp_path, "composer.json", COMPOSER_JSON, more.composer_json))
    assert sorted(deps) == ["guzzlehttp/guzzle", "monolog/monolog", "phpunit/phpunit"]
    assert deps["guzzlehttp/guzzle"]["version"] == "7.4.5" and deps["monolog/monolog"]["version"] is None and deps["monolog/monolog"]["declared"] == "^2.0"


@pytest.mark.parametrize("name, reader", [("composer.lock", more.composer_lock), ("composer.json", more.composer_json)])
def test_broken_json_is_a_sentence_and_not_a_crash(tmp_path, name, reader):
    with pytest.raises(mf.ManifestError, match="not valid JSON"):
        read(tmp_path, name, "{nope", reader)


def test_the_readers_are_found_by_file_name_and_feed_the_merge_like_any_other(tmp_path):
    (tmp_path / "go.mod").write_text(GO_MOD, encoding="utf-8")
    (tmp_path / "Gemfile.lock").write_text(GEM_LOCK, encoding="utf-8")
    for name in ("go.mod", "pom.xml", "gradle.lockfile", "Gemfile", "Gemfile.lock", "composer.json", "composer.lock"):
        assert mf.reader_for(name) is not None, name
    deps = mf.read(tmp_path, "go.mod") + mf.read(tmp_path, "Gemfile.lock")
    rows = mf.merge(deps)
    assert {(r["ecosystem"], r["name"]) for r in rows} >= {("Go", "github.com/pkg/errors"), ("RubyGems", "rack")}
    assert mf.reader_for("go.sum") is None, "go.sum is hashes only, and is reported as not read"


# -- crates.io, NuGet and Hex ---------------------------------------------------------------------------------------------------------------------------------

CARGO_LOCK = """version = 3

[[package]]
name = "serde"
version = "1.0.152"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "abc"

[[package]]
name = "shop"
version = "0.1.0"
dependencies = ["serde"]

[[package]]
name = "gitdep"
version = "0.2.0"
source = "git+https://github.com/x/y#abc"
"""

CARGO_TOML = """[package]
name = "shop"
version = "0.1.0"

[dependencies]
serde = "1.0"
tokio = { version = "=1.25.0", features = ["full"] }
localdep = { path = "../localdep" }
gitdep = { git = "https://github.com/x/y" }
renamed = { package = "real-name", version = "2" }

[dev-dependencies]
rand = "0.8.5"

[target.'cfg(windows)'.dependencies]
winapi = "0.3"

[workspace.dependencies]
anyhow = "1"
"""

NUGET_LOCK = '{"version": 1, "dependencies": {"net6.0": {"Newtonsoft.Json": {"type": "Direct", "requested": "[13.0.1, )", "resolved": "13.0.1", "contentHash": "x"}, "Shop.Core": {"type": "Project"}, "Serilog": {"type": "Transitive", "resolved": "2.12.0"}}}}'

PACKAGES_CONFIG = '<?xml version="1.0" encoding="utf-8"?><packages><package id="Newtonsoft.Json" version="12.0.3" targetFramework="net48" /><package id="NoVersion" targetFramework="net48" /></packages>'

CSPROJ = """<Project Sdk="Microsoft.NET.Sdk">
  <ItemGroup>
    <PackageReference Include="Newtonsoft.Json" Version="13.0.1" />
    <PackageReference Include="Ranged" Version="[1.0,2.0)" />
    <PackageReference Include="Floating" Version="1.*" />
    <PackageReference Include="Central" />
    <PackageReference Include="Child"><Version>3.1.0</Version></PackageReference>
    <PackageReference Include="Property" Version="$(PropVersion)" />
  </ItemGroup>
</Project>"""

MIX_LOCK = """%{
  "phoenix": {:hex, :phoenix, "1.6.15", "0ceda3", [:mix], [{:jason, "~> 1.0", [hex: :jason, repo: "hexpm", optional: true]}], "hexpm", "abc"},
  "gitpkg": {:git, "https://github.com/x/y.git", "abc123", []},
  "plug_crypto": {:hex, :plug_crypto, "1.2.3", "aaa", [:mix], [], "hexpm", "bbb"},
}
"""

MIX_EXS = """defmodule Shop.MixProject do
  defp deps do
    [
      {:phoenix, "~> 1.6.0"},
      {:ecto, "3.9.1"},
      {:jason, ">= 1.0.0", only: :dev},
      {:local, path: "../local"},
      {:gitpkg, git: "https://github.com/x/y.git"},
      {:thing, "~> 1.0", git: "https://github.com/x/z.git"},
      {:poison, "== 4.0.1"} # exact
    ]
  end
end
"""


def test_cargo_lock_lists_registry_crates_only(tmp_path):
    deps = by_name(read(tmp_path, "Cargo.lock", CARGO_LOCK, more.cargo_lock))
    assert list(deps) == ["serde"] and (deps["serde"]["version"], deps["serde"]["source"], deps["serde"]["ecosystem"]) == ("1.0.152", "lock", "crates.io")


def test_cargo_toml_a_bare_version_is_a_caret_range_and_only_an_equals_is_exact(tmp_path):
    deps = by_name(read(tmp_path, "Cargo.toml", CARGO_TOML, more.cargo_toml))
    assert sorted(deps) == ["anyhow", "rand", "real-name", "serde", "tokio", "winapi"]
    assert deps["tokio"]["version"] == "1.25.0" and deps["tokio"]["source"] == "pinned"
    assert deps["serde"]["version"] is None and deps["serde"]["declared"] == "1.0" and deps["rand"]["version"] is None, "0.8.5 in Cargo means ^0.8.5"
    assert "localdep" not in deps and "gitdep" not in deps and "renamed" not in deps, "path and git dependencies are not registry packages; a renamed one is listed by its real name"


def test_nuget_lock_files_give_the_resolved_versions_and_skip_project_references(tmp_path):
    deps = by_name(read(tmp_path, "packages.lock.json", NUGET_LOCK, more.nuget_lock))
    assert sorted(deps) == ["Newtonsoft.Json", "Serilog"]
    assert (deps["Newtonsoft.Json"]["version"], deps["Newtonsoft.Json"]["declared"], deps["Newtonsoft.Json"]["source"]) == ("13.0.1", "[13.0.1, )", "lock")
    deps = by_name(read(tmp_path, "packages.config", PACKAGES_CONFIG, more.nuget_packages_config))
    assert list(deps) == ["Newtonsoft.Json"] and deps["Newtonsoft.Json"]["version"] == "12.0.3"


def test_a_project_file_gives_versions_ranges_or_none_under_central_management(tmp_path):
    deps = by_name(read(tmp_path, "Shop.csproj", CSPROJ, more.nuget_project))
    assert deps["Newtonsoft.Json"]["version"] == "13.0.1" and deps["Child"]["version"] == "3.1.0"
    for name in ("Ranged", "Floating", "Central", "Property"):
        assert deps[name]["version"] is None and deps[name]["source"] == "declared", name
    assert deps["Ranged"]["declared"] == "[1.0,2.0)" and deps["Central"]["declared"] is None
    props = '<Project><ItemGroup><PackageVersion Include="Central" Version="4.5.6" /></ItemGroup></Project>'
    assert by_name(read(tmp_path, "Directory.Packages.props", props, more.nuget_project))["Central"]["version"] == "4.5.6"


def test_mix_lock_lists_hex_packages_and_not_git_ones(tmp_path):
    deps = by_name(read(tmp_path, "mix.lock", MIX_LOCK, more.mix_lock))
    assert sorted(deps) == ["phoenix", "plug_crypto"] and (deps["phoenix"]["version"], deps["phoenix"]["source"], deps["phoenix"]["ecosystem"]) == ("1.6.15", "lock", "Hex")


def test_mix_exs_a_plain_version_is_exact_in_hex_and_a_range_is_not(tmp_path):
    deps = by_name(read(tmp_path, "mix.exs", MIX_EXS, more.mix_exs))
    assert sorted(deps) == ["ecto", "jason", "phoenix", "poison"]
    assert deps["ecto"]["version"] == "3.9.1" and deps["poison"]["version"] == "4.0.1"
    assert deps["phoenix"]["version"] is None and deps["phoenix"]["declared"] == "~> 1.6.0" and deps["jason"]["declared"] == ">= 1.0.0"


@pytest.mark.parametrize("name, reader", [("Cargo.lock", more.cargo_lock), ("Cargo.toml", more.cargo_toml)])
def test_broken_toml_is_a_sentence_and_not_a_crash(tmp_path, name, reader):
    with pytest.raises(mf.ManifestError, match="not valid TOML"):
        read(tmp_path, name, "[[package", reader)


@pytest.mark.parametrize("name, reader", [("packages.config", more.nuget_packages_config), ("Shop.csproj", more.nuget_project)])
def test_nuget_xml_with_a_document_type_is_refused(tmp_path, name, reader):
    with pytest.raises(mf.ManifestError, match="document type or an entity"):
        read(tmp_path, name, '<!DOCTYPE x [<!ENTITY a "b">]><packages/>', reader)


def test_every_reader_is_found_by_file_name_or_suffix(tmp_path):
    for name in ("Cargo.lock", "Cargo.toml", "packages.lock.json", "packages.config", "Directory.Packages.props", "mix.lock", "mix.exs", "Shop.csproj", "Lib.fsproj", "Old.vbproj"):
        assert mf.reader_for(name) is not None, name
    assert mf.reader_for("Project.sln") is None


def test_a_pom_whose_properties_expand_into_each_other_many_times_over_is_left_unresolved_and_is_fast(tmp_path):
    import time
    refs = "${p1}" * 40
    props = "".join(f"<p{i}>{'${p' + str(i + 1) + '}' * 40}</p{i}>" for i in range(1, 8)) + "<p8>1.0</p8>"
    pom = ('<project xmlns="http://maven.apache.org/POM/4.0.0"><properties>' + props + '</properties><dependencies>'
           '<dependency><groupId>g</groupId><artifactId>a</artifactId><version>${p1}</version></dependency></dependencies></project>')
    started = time.perf_counter()
    (dep,) = read(tmp_path, "pom.xml", pom, more.maven_pom)
    assert time.perf_counter() - started < 2.0, "40 references to 40 references, seven levels deep, would be 40 to the seventh power if it were built"
    assert dep["version"] is None and dep["source"] == "declared" and dep["declared"] and len(dep["declared"]) <= more.MAX_RESOLVED and "${" in dep["declared"]
    assert refs.startswith("${p1}")


def test_a_property_of_megabytes_used_many_times_is_never_built_and_costs_no_memory_to_speak_of(tmp_path):
    import time
    import tracemalloc
    big = "x" * (5 * 1024 * 1024)
    pom = ('<project xmlns="http://maven.apache.org/POM/4.0.0"><properties><big>' + big + '</big></properties><dependencies>'
           '<dependency><groupId>g</groupId><artifactId>a</artifactId><version>' + "${big}" * 300 + '</version></dependency></dependencies></project>')
    (tmp_path / "pom.xml").write_text(pom, encoding="utf-8")
    tracemalloc.start()
    started = time.perf_counter()
    (dep,) = more.maven_pom(tmp_path / "pom.xml", "pom.xml")
    elapsed, (_, peak) = time.perf_counter() - started, tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert dep["version"] is None and dep["source"] == "declared" and len(dep["declared"] or "") <= more.MAX_RESOLVED
    assert peak < 60 * 1024 * 1024, f"peak {peak:,} bytes: 300 copies of 5 MB (1.5 GB) must not be built"
    assert elapsed < 5.0
