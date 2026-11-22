"""Reading the manifests and lock files of npm and Python projects: the exact version in use when a file says it, the declared range when it does not, and a sentence for a file that
cannot be read. Nothing is executed; the files are only read."""
import json

import pytest

from app.backend.pipeline.scan import manifests as mf


def put(root, name, text):
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text if isinstance(text, str) else json.dumps(text), encoding="utf-8")
    return name


def names(deps):
    return sorted((d["name"], d["version"], d["source"]) for d in deps)


def test_package_json_gives_the_range_and_the_exact_version_only_when_it_is_written_exactly(tmp_path):
    put(tmp_path, "package.json", {"dependencies": {"left-pad": "1.3.0", "express": "^4.18.0", "lodash": "~4.17.0", "tag": "latest", "v": "v2.0.1"},
                                   "devDependencies": {"jest": ">=29 <30"}, "scripts": {"x": "y"}})
    d = mf.read(tmp_path, "package.json")
    assert names(d) == [("express", None, "declared"), ("jest", None, "declared"), ("left-pad", "1.3.0", "pinned"), ("lodash", None, "declared"), ("tag", None, "declared"), ("v", "2.0.1", "pinned")]
    assert {x["name"]: x["declared"] for x in d}["express"] == "^4.18.0" and all(x["ecosystem"] == "npm" and x["manifest"] == "package.json" for x in d)


def test_package_lock_v3_and_v2_list_every_package_in_the_tree_with_its_version_and_skip_the_root_and_links(tmp_path):
    put(tmp_path, "package-lock.json", {"lockfileVersion": 3, "packages": {
        "": {"name": "app", "version": "1.0.0"}, "node_modules/express": {"version": "4.18.2"}, "node_modules/express/node_modules/qs": {"version": "6.11.0"},
        "node_modules/@scope/pkg": {"version": "2.0.0", "dev": True}, "node_modules/linked": {"link": True, "version": "9.9.9"}, "node_modules/alias": {"name": "real-name", "version": "1.0.0"}}})
    assert names(mf.read(tmp_path, "package-lock.json")) == [("@scope/pkg", "2.0.0", "lock"), ("express", "4.18.2", "lock"), ("qs", "6.11.0", "lock"), ("real-name", "1.0.0", "lock")]


def test_package_lock_v1_is_a_tree_of_dependencies_and_a_local_or_git_source_is_skipped(tmp_path):
    put(tmp_path, "package-lock.json", {"lockfileVersion": 1, "dependencies": {"a": {"version": "1.0.0", "dependencies": {"b": {"version": "2.0.0"}}},
                                                                                "c": {"version": "file:../c"}, "d": {"version": "git+https://x/y.git"}}})
    assert names(mf.read(tmp_path, "package-lock.json")) == [("a", "1.0.0", "lock"), ("b", "2.0.0", "lock")]


def test_yarn_lock_v1_and_berry_give_the_resolved_version_of_each_block(tmp_path):
    put(tmp_path, "yarn.lock", '# yarn lockfile v1\n\n"@babel/core@^7.0.0", "@babel/core@^7.1.0":\n  version "7.2.1"\n  resolved "x"\n\nlodash@^4.17.0:\n  version "4.17.21"\n')
    assert names(mf.read(tmp_path, "yarn.lock")) == [("@babel/core", "7.2.1", "lock"), ("lodash", "4.17.21", "lock")]
    put(tmp_path, "yarn.lock", '__metadata:\n  version: 6\n\n"react@npm:^18.0.0":\n  version: 18.2.0\n  resolution: "react@npm:18.2.0"\n')
    assert ("react", "18.2.0", "lock") in names(mf.read(tmp_path, "yarn.lock"))


def test_pnpm_lock_keys_of_the_old_and_new_forms_give_name_and_version(tmp_path):
    put(tmp_path, "pnpm-lock.yaml", "lockfileVersion: 5.4\npackages:\n  /express/4.18.2:\n    resolution: {}\n  /@scope/pkg/2.0.0:\n    resolution: {}\n")
    assert names(mf.read(tmp_path, "pnpm-lock.yaml")) == [("@scope/pkg", "2.0.0", "lock"), ("express", "4.18.2", "lock")]
    put(tmp_path, "pnpm-lock.yaml", "lockfileVersion: '9.0'\npackages:\n  'react@18.2.0':\n    resolution: {}\n  '@x/y@1.0.0(react@18.2.0)':\n    resolution: {}\n")
    assert names(mf.read(tmp_path, "pnpm-lock.yaml")) == [("@x/y", "1.0.0", "lock"), ("react", "18.2.0", "lock")]


def test_requirements_pins_and_ranges_comments_and_options(tmp_path):
    put(tmp_path, "requirements.txt", "# top\nflask==2.0.1  # web\nRequests>=2.0\nnumpy\n-r other.txt\n--index-url https://x\ngit+https://github.com/a/b.git\nbad line ===\nDjango[argon2]==4.2\n")
    d = mf.read(tmp_path, "requirements.txt")
    assert names(d) == [("django", "4.2", "pinned"), ("flask", "2.0.1", "pinned"), ("numpy", None, "declared"), ("requests", None, "declared")]
    assert {x["name"]: x["declared"] for x in d}["requests"] == ">=2.0"


def test_poetry_lock_pipfile_lock_and_pyproject(tmp_path):
    put(tmp_path, "poetry.lock", '[[package]]\nname = "Flask"\nversion = "2.0.1"\n\n[[package]]\nname = "click"\nversion = "8.1.0"\n')
    assert names(mf.read(tmp_path, "poetry.lock")) == [("click", "8.1.0", "lock"), ("flask", "2.0.1", "lock")]
    put(tmp_path, "Pipfile.lock", {"default": {"Django": {"version": "==4.2.1"}}, "develop": {"pytest": {"version": "==7.0.0"}, "git-dep": {"git": "https://x"}}})
    assert names(mf.read(tmp_path, "Pipfile.lock")) == [("django", "4.2.1", "lock"), ("git-dep", None, "declared"), ("pytest", "7.0.0", "lock")]
    put(tmp_path, "pyproject.toml", '[project]\ndependencies = ["httpx==0.27.0", "rich>=13"]\n\n[tool.poetry.dependencies]\npython = "^3.11"\nattrs = "23.1.0"\ntoml = {version = "^0.10"}\n')
    assert names(mf.read(tmp_path, "pyproject.toml")) == [("attrs", "23.1.0", "pinned"), ("httpx", "0.27.0", "pinned"), ("rich", None, "declared"), ("toml", None, "declared")]


def test_a_file_that_is_damaged_too_large_or_missing_is_refused_in_words(tmp_path, monkeypatch):
    put(tmp_path, "package.json", "{not json")
    with pytest.raises(mf.ManifestError, match="package.json is not valid JSON"):
        mf.read(tmp_path, "package.json")
    put(tmp_path, "pyproject.toml", "[broken")
    with pytest.raises(mf.ManifestError, match="not valid TOML"):
        mf.read(tmp_path, "pyproject.toml")
    with pytest.raises(mf.ManifestError, match="could not be read"):
        mf.read(tmp_path, "yarn.lock")
    monkeypatch.setattr(mf, "MAX_BYTES", 5)
    put(tmp_path, "requirements.txt", "flask==2.0.1\n")
    with pytest.raises(mf.ManifestError, match="larger than"):
        mf.read(tmp_path, "requirements.txt")


def test_a_manifest_of_a_kind_that_is_not_read_yet_gives_nothing_and_the_names_of_requirements_files_are_recognised(tmp_path):
    put(tmp_path, "Package.swift", "// swift-tools-version:5.9")        # Swift is not one of the ecosystems whose manifests are read
    assert mf.read(tmp_path, "Package.swift") == [] and mf.reader_for("Package.swift") is None
    for name in ("requirements.txt", "requirements-dev.txt", "requirements_test.txt", "requirements.prod.txt", "constraints.txt"):
        assert mf.reader_for(name) is mf.py_requirements, name
    assert mf.reader_for("sub/requirements.txt".split("/")[-1]) is mf.py_requirements


def test_one_row_per_package_and_version_in_use_the_lock_wins_over_the_range_and_manifests_are_collected(tmp_path):
    put(tmp_path, "package.json", {"dependencies": {"express": "^4.18.0", "left-pad": "^1.0.0"}})
    put(tmp_path, "web/package.json", {"dependencies": {"express": "^4.0.0"}})
    put(tmp_path, "package-lock.json", {"lockfileVersion": 3, "packages": {"node_modules/express": {"version": "4.18.2"}, "node_modules/express/node_modules/express": {"version": "3.0.0"}}})
    deps = mf.merge(mf.read(tmp_path, "package.json") + mf.read(tmp_path, "web/package.json") + mf.read(tmp_path, "package-lock.json"))
    rows = [(d["name"], d["version"], d["declared"], d["source"]) for d in deps]
    assert rows == [("express", "3.0.0", None, "lock"), ("express", "4.18.2", None, "lock"), ("left-pad", None, "^1.0.0", "declared")]      # express by range is not listed twice more
    assert deps[2]["manifests"] == ["package.json"]
    two = mf.merge([{"ecosystem": "npm", "name": "a", "version": "1.0.0", "declared": "1.0.0", "manifest": "x/package.json", "source": "pinned"},
                    {"ecosystem": "npm", "name": "a", "version": "1.0.0", "declared": None, "manifest": "package-lock.json", "source": "lock"}])
    assert len(two) == 1 and two[0]["manifests"] == ["x/package.json", "package-lock.json"] and two[0]["source"] == "lock"
