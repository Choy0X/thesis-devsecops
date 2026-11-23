"""Mining: parent decisions, commit kinds, all languages, file lists and pagination, licenses."""
import hashlib
import json

import pytest

from app.backend.pipeline import codestore, ecosystems as eco, store
from app.backend.pipeline.ingest import build_code_dataset as bcd
from app.backend.pipeline.ingest import codemine, licenses

SHA, PARENT = "s" * 40, "p" * 40
ROW = {"commit_key": "gh:o/r@s", "repo_url": "https://github.com/o/r", "expected_langs": "[]", "sources": '["osv"]'}


class Loader:
    def __init__(self, files=None):
        self.files = files or {}

    def content(self, ref, path):
        return self.files.get((ref, path), b"class A { void f() { x(); } }\n" if ref == PARENT
                              else b"class A { void f() { y(); } }\n")

    def diff(self, *a):
        return None

    def repo_profile(self, sha):
        return {}


def commit(paths, message="fix", parents=(PARENT,), statuses=None, loader=None):
    files = [codemine.FileChange(p, p, (statuses or {}).get(p, "modified"), patch=f"@@ {p}") for p in paths]
    return codemine.Commit(sha=SHA, parent=parents[0] if parents else None, message=message,
                           date="2026-01-01T00:00:00Z", files=files, loader=loader or Loader(),
                           parents=list(parents))


@pytest.fixture(autouse=True)
def _no_grammars(monkeypatch):
    monkeypatch.setattr(codemine, "sub_pairs_with_analysis", lambda *a, **k: ([], None))      # no tree-sitter grammar needed


def mine(paths, meta=None, row=None, **kw):
    return codemine.build_pairs({**ROW, **(row or {})}, commit(paths, **kw), meta or {"ecosystem": "npm"}, max_files=40)


# -- commit kinds and the parent decision -------------------------------------------------------------------

@pytest.mark.parametrize("message,parents,files,kind,reason,quarantine", [
    ("Fix XSS", 1, ["a.js"], "normal", "single_parent", None),
    ("Revert \"Fix XSS\"\n\nThis reverts commit abc.", 1, ["a.js"], "revert", "single_parent", "revert_commit"),
    ("bump version to 1.2.3", 1, ["package.json", "CHANGELOG.md"], "release", "single_parent", "release_commit"),
    ("bump deps", 1, ["a.js", "package.json"], "normal", "single_parent", None),      # touches code: not a pure release
    ("Merge pull request #12 from x/fix", 2, ["a.js"], "merge_pr", "merge_first_parent_net_diff", None),
    ("Merge commit from fork", 2, ["a.js"], "merge_pr", "merge_first_parent_net_diff", None),
    ("Merge branch 'main' into feature", 2, ["a.js"], "merge_branch", "merge_ambiguous_parent", "ambiguous_merge_parent"),
    ("Merge tag 'v1.2'", 2, ["a.js"], "merge_branch", "merge_ambiguous_parent", "ambiguous_merge_parent"),
    ("combine histories", 2, ["a.js"], "merge_other", "merge_ambiguous_parent", "ambiguous_merge_parent"),
])
def test_commit_kind_and_parent_decision(message, parents, files, kind, reason, quarantine):
    got = codemine.classify_commit(message, parents, files, "npm")
    assert (got["kind"], got["reason"], got["quarantine"]) == (kind, reason, quarantine)
    assert got["parent_count"] == parents


def test_pairs_record_the_parent_decision_and_quarantine_ambiguous_commits():
    ok = mine(["src/a.js"], message="fix", parents=(PARENT,))
    assert {(p["parent_count"], p["parent_selection_reason"], p["quarantine_reason"]) for p in ok.pairs} == {
        (1, "single_parent", None)}
    merge = mine(["src/a.js"], message="Merge branch 'dev'", parents=(PARENT, "q" * 40))
    assert {p["quarantine_reason"] for p in merge.pairs} == {"ambiguous_merge_parent"}
    assert {p["parent_count"] for p in merge.pairs} == {2}
    assert all(p["code_available"] == 1 for p in merge.pairs)             # kept, just not trusted


def test_the_extraction_version_old_path_and_blob_hashes_are_recorded():
    out = mine(["src/a.js"])
    p = out.pairs[0]
    assert p["extraction_version"] == codemine.EXTRACTION_VERSION
    assert p["old_path"] == "src/a.js"
    assert p["before_blob_sha"] == codemine.git_blob_sha(b"class A { void f() { x(); } }\n")
    assert p["after_blob_sha"] == codemine.git_blob_sha(b"class A { void f() { y(); } }\n")


def test_git_blob_hashes_match_git():
    assert codemine.git_blob_sha(b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
    assert codemine.git_blob_sha(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"


# -- every language is mined ---------------------------------------------------------------------------------

def test_sibling_language_files_are_mined_not_dropped():
    out = mine(["src/a.js", "scripts/build.py", "tools/x.go"])
    assert {p["language"] for p in out.pairs} == {"javascript", "python", "go"}


def test_a_language_set_on_the_record_still_filters():
    out = mine(["src/a.js", "scripts/build.py"], row={"expected_langs": '["javascript"]'})
    assert {p["language"] for p in out.pairs} == {"javascript"}


def test_new_extensions_are_known():
    for ext, lang in ((".mts", "typescript"), (".cts", "typescript"), (".sol", "solidity"), (".vy", "vyper"),
                      (".html", "html"), (".ejs", "ejs"), (".hbs", "handlebars"), (".pug", "pug")):
        assert codemine.language_of(f"x{ext}") == lang


# -- tangled commits ---------------------------------------------------------------------------------------------

def fake_functions(monkeypatch, n_prod, n_test):
    def sub(file_pair, old_path=None, policy=None):
        n = n_test if file_pair["is_test"] else n_prod
        return [{**file_pair, "granularity": "function", "function_name": f"f{i}", "pair_key": f"{file_pair['pair_key']}{i}",
                 "code_available": 1} for i in range(n)], None
    monkeypatch.setattr(codemine, "sub_pairs_with_analysis", sub)


def test_test_functions_do_not_make_a_commit_tangled(monkeypatch):
    fake_functions(monkeypatch, n_prod=5, n_test=30)
    out = mine(["src/a.js", "test/a.test.js"])
    assert out.reason is None and {p["tangled"] for p in out.pairs} == {0}


def test_many_production_functions_do_make_a_commit_tangled(monkeypatch):
    fake_functions(monkeypatch, n_prod=25, n_test=0)
    out = mine(["src/a.js", "test/a.test.js"])
    assert out.reason == "tangled_commit"
    assert {p["tangled"] for p in out.pairs if not p["is_test"]} == {1}
    assert {p["tangled"] for p in out.pairs if p["is_test"]} == {0}        # a test pair is never "tangled"


# -- the file list --------------------------------------------------------------------------------------------------

def test_every_changed_file_is_listed_with_its_category():
    out = mine(["src/a.js", "package.json", "docs/guide.md", "package-lock.json", "test/a.test.js",
                "node_modules/x/i.js", "types/a.d.ts", ".github/workflows/ci.yml"], meta={"ecosystem": "npm"})
    cats = {f["path"]: f["category"] for f in out.files}
    assert cats == {"src/a.js": "source", "package.json": "manifest", "docs/guide.md": "docs",
                    "package-lock.json": "manifest", "test/a.test.js": "test", "node_modules/x/i.js": "vendor",
                    "types/a.d.ts": "generated", ".github/workflows/ci.yml": "config"}


def test_patches_are_kept_only_for_configuration_and_manifest_files():
    out = mine(["src/a.js", "package.json", ".github/workflows/ci.yml", "docs/guide.md"])
    kept = {f["path"] for f in out.files if f["patch"]}
    assert kept == {"package.json", ".github/workflows/ci.yml"}


def test_a_git_sourced_file_with_no_patch_builds_its_diff_from_the_already_fetched_text_not_a_subprocess():
    # GitFetcher never sets patch on its FileChange (only GitHub's API does): this is the path every git-sourced commit takes. before/after are
    # already fetched by the time the diff is built, so loader.diff (a locked git subprocess) must never be called at all.
    calls = []

    class NoPatchLoader(Loader):
        def diff(self, *a):
            calls.append(a)
            return "loader.diff's own output, which must never be used here"

    files = [codemine.FileChange("src/a.js", "src/a.js", "modified", patch=None)]
    c = codemine.Commit(sha=SHA, parent=PARENT, message="fix", date="2026-01-01T00:00:00Z",
                        files=files, loader=NoPatchLoader(), parents=[PARENT])
    out = codemine.build_pairs({**ROW}, c, {"ecosystem": "npm"}, max_files=40)
    assert calls == [], "loader.diff must not be called once before/after content is already fetched"
    before, after = "class A { void f() { x(); } }\n", "class A { void f() { y(); } }\n"
    expected = codemine._udiff(before, after, "a/src/a.js", "b/src/a.js")
    stored = next(p for p in out.pairs if p["file_path"] == "src/a.js" and p["granularity"] == "file")
    assert stored["diff"] == expected


def test_the_file_list_is_stored_with_the_commit_result(workspace):
    out = mine(["src/a.js", "package.json"])
    codemine._store_result({**ROW, "owner": "o", "repo": "r", "sha": SHA}, out, __import__("collections").Counter(), "o/r")
    got = codestore.commit_files("gh:o/r@s")
    assert {(f["path"], f["category"]) for f in got} == {("src/a.js", "source"), ("package.json", "manifest")}
    codemine._store_result({**ROW, "owner": "o", "repo": "r", "sha": SHA}, out, __import__("collections").Counter(), "o/r")
    assert len(codestore.commit_files("gh:o/r@s")) == 2                      # replaced, not duplicated


def test_storing_a_mined_commit_stores_the_signals_and_change_key_of_each_pair_beside_it(workspace):
    from app.backend.pipeline.ingest import pairquality
    out = mine(["src/a.js", "test/a.test.js"])
    codemine._store_result({**ROW, "owner": "o", "repo": "r", "sha": SHA}, out, __import__("collections").Counter(), "o/r")
    keys = [p["pair_key"] for p in out.pairs if p.get("code_available")]
    got = codestore.pair_quality_for(keys)
    assert keys and set(got) == set(keys)
    assert all(q["version"] == pairquality.QUALITY_VERSION and isinstance(q["signals"], list) for q in got.values())
    assert codestore.attribution_commit_keys(True, version=pairquality.QUALITY_VERSION) == [], "mining leaves nothing for the attribution pass to do"


# -- GitHub: pagination and licenses -------------------------------------------------------------------------------------

class Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self._b, self.headers = status, body, headers or {}
        self.text = json.dumps(body)

    def json(self):
        return self._b

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class Session:
    def __init__(self, routes):
        self.routes, self.calls, self.headers = routes, [], {}

    def get(self, url, **kw):
        self.calls.append((url, kw.get("params")))
        key = url.split("api.github.com")[-1]
        page = (kw.get("params") or {}).get("page", 1)
        r = self.routes.get((key, page)) or self.routes.get(key)
        return r if r is not None else Resp(404, {"message": "not found"})


def gh_file(i):
    return {"filename": f"f{i}.js", "status": "modified", "patch": "@@", "additions": 2, "deletions": 1,
            "sha": f"{i:040d}"}


def fetcher(session):
    f = codemine.GitHubFetcher("token")
    f.s = session
    return f


def test_a_commit_with_more_than_300_files_is_fetched_page_by_page():
    path = f"/repos/o/r/commits/{SHA}"
    s = Session({(path, 1): Resp(200, {"sha": SHA, "parents": [{"sha": PARENT}], "commit": {"message": "m", "committer": {"date": "d"}},
                                       "files": [gh_file(i) for i in range(300)]}),
                 (path, 2): Resp(200, {"sha": SHA, "files": [gh_file(i) for i in range(300, 410)]})})
    c = fetcher(s).commit({"owner": "o", "repo": "r", "sha": SHA})
    assert len(c.files) == 410 and c.files[0].additions == 2 and c.files[0].blob_sha == f"{0:040d}"
    assert [p for _, p in s.calls if p] == [{"page": 2, "per_page": 300}] or len(s.calls) == 2
    assert c.parents == [PARENT]


def test_a_small_commit_needs_one_request():
    path = f"/repos/o/r/commits/{SHA}"
    s = Session({path: Resp(200, {"sha": SHA, "parents": [{"sha": PARENT}, {"sha": "q" * 40}],
                                  "commit": {"message": "m", "committer": {"date": "d"}}, "files": [gh_file(1)]})})
    c = fetcher(s).commit({"owner": "o", "repo": "r", "sha": SHA})
    assert len(s.calls) == 1 and c.parents == [PARENT, "q" * 40] and c.parent == PARENT


@pytest.mark.parametrize("spdx,status,redistributable", [
    ("MIT", "permissive", True), ("Apache-2.0", "permissive", True), ("BSD-3-Clause", "permissive", True),
    ("ISC", "permissive", True), ("Unlicense", "permissive", True),
    ("GPL-3.0", "copyleft", False), ("AGPL-3.0-only", "copyleft", False), ("LGPL-2.1", "copyleft", False),
    ("MPL-2.0", "copyleft", False), ("NOASSERTION", "unknown", False), (None, "unknown", False),
    ("", "unknown", False), ("SomethingCustom", "unknown", False)])
def test_license_status(spdx, status, redistributable):
    assert licenses.status(spdx) == status
    assert licenses.redistributable(spdx) is redistributable


def test_a_repository_license_is_fetched_once_and_stored(workspace):
    s = Session({"/repos/o/r/license": Resp(200, {"license": {"spdx_id": "MIT"}})})
    f = fetcher(s)
    assert licenses.for_repository(f, "github.com", "o", "r") == ("MIT", "permissive")
    assert licenses.for_repository(f, "github.com", "o", "r") == ("MIT", "permissive")
    assert len(s.calls) == 1
    assert codestore.repository("github.com/o/r")["license_spdx"] == "MIT"


def test_a_repository_without_a_license_is_recorded_as_unknown(workspace):
    f = fetcher(Session({}))
    assert licenses.for_repository(f, "github.com", "o", "none") == (None, "unknown")
    assert codestore.repository("github.com/o/none")["data_usage_status"] == "unknown"


def test_other_hosts_are_not_looked_up(workspace):
    f = fetcher(Session({}))
    assert licenses.for_repository(f, "gitlab.com", "o", "r") == (None, "unknown")
    assert f.s.calls == []


# -- migrations -------------------------------------------------------------------------------------------------------------

def seed_pairs(workspace):
    conn = store.open_db()
    store._setup(conn)
    codestore._setup(conn)
    def pair(key, commit_key, fn, test=0, tangled=0):
        conn.execute("INSERT INTO code_pairs (pair_key, commit_key, granularity, function_name, is_test, tangled, "
                     "code_available, code_before, code_after, diff) VALUES (?,?,?,?,?,?,1,'a','b','d')",
                     (key, commit_key, "function", fn, test, tangled))
    for i in range(25):
        pair(f"A{i}", "c-tests", f"t{i}", test=1, tangled=1)           # 25 test functions: wrongly tangled
    for i in range(3):
        pair(f"B{i}", "c-tests", f"p{i}", test=0, tangled=1)           # 3 production functions: wrongly tangled
    for i in range(25):
        pair(f"C{i}", "c-big", f"p{i}", test=0, tangled=0)             # 25 production functions: should be tangled
    conn.commit()
    conn.close()


# -- the export -------------------------------------------------------------------------------------------------------------

def test_the_export_defaults_keep_sibling_languages_and_drop_quarantined_pairs():
    assert bcd.DEFAULTS["sibling_languages"] is True
    assert bcd.DEFAULTS["include_quarantined"] is False
    from app.backend.pipeline import lightpairs
    assert "quarantine_reason" in lightpairs.FIELDS


def test_the_ecosystem_adapters_classify_config_and_manifest_files():
    assert eco.categorize_path("package.json", "npm", None) == "manifest"
    assert eco.categorize_path("pom.xml", "Maven", None) == "manifest"
    assert eco.categorize_path("config/app.yml", "npm", None) == "config"
    assert eco.categorize_path("Dockerfile", "PyPI", None) == "config"
    assert eco.categorize_path("src/app.py", "PyPI", "python") == "source"
    assert eco.categorize_path("README.md", None, None) == "docs"
    assert eco.categorize_path("assets/logo.png", "npm", None) == "other"


# -- a merge whose tree is its second parent's tree ----------------------------------------------------------------------------------

@pytest.mark.parametrize("message,parents,files,tree_equal,kind,reason,quarantine", [
    ("Merge branch 'develop' into master", 2, 12, True, "merge_pr", "merge_first_parent_tree_equal", None),     # the merge added nothing beyond the fix branch
    ("combine histories", 2, 3, True, "merge_pr", "merge_first_parent_tree_equal", None),
    ("Merge branch 'main' into feature", 2, 3, False, "merge_branch", "merge_ambiguous_parent", "ambiguous_merge_parent"),    # a sync merge: it brings in other work
    ("Merge branch 'main' into feature", 2, 3, None, "merge_branch", "merge_ambiguous_parent", "ambiguous_merge_parent"),     # unknown stays unsafe
    ("Merge branch 'develop' into master", 2, 21, True, "merge_branch", "merge_ambiguous_parent", "ambiguous_merge_parent"),  # too many files to be one fix
    ("Merge branches", 3, 3, True, "merge_other", "merge_ambiguous_parent", "ambiguous_merge_parent"),                       # an octopus merge
    ("Merge pull request #1 from x/y", 2, 3, False, "merge_pr", "merge_first_parent_net_diff", None),                         # the old rule is untouched
])
def test_a_two_parent_merge_that_added_nothing_to_its_second_parent_is_the_net_change_of_the_fix(message, parents, files, tree_equal, kind, reason, quarantine):
    got = codemine.classify_commit(message, parents, [f"src/f{i}.js" for i in range(files)], "npm", tree_equal=tree_equal)
    assert (got["kind"], got["reason"], got["quarantine"]) == (kind, reason, quarantine)


def test_pairs_of_such_a_merge_are_not_quarantined():
    c = commit(["src/a.js"], message="Merge branch 'develop' into master", parents=(PARENT, "q" * 40))
    sync = codemine.build_pairs(ROW, c, {"ecosystem": "npm"}, max_files=40)
    assert {p["quarantine_reason"] for p in sync.pairs} == {"ambiguous_merge_parent"}
    c.tree_equal = True
    fix = codemine.build_pairs(ROW, c, {"ecosystem": "npm"}, max_files=40)
    assert {p["quarantine_reason"] for p in fix.pairs} == {None} and {p["parent_selection_reason"] for p in fix.pairs} == {"merge_first_parent_tree_equal"}
    assert fix.kind["kind"] == "merge_pr" and all(p["code_available"] == 1 for p in fix.pairs)


def git_in(cwd, *args):
    import subprocess
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def merges(tmp_path):
    """A repository with two merges: one of a fix branch into an untouched master (the tree is the fix branch's) and one of master into a branch that had moved on (it brings in other work)."""
    import shutil
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    work = tmp_path / "work"
    work.mkdir()
    git_in(work, "init", "-q", "-b", "master")
    (work / "a.js").write_text("one\n")
    git_in(work, "add", ".")
    git_in(work, "commit", "-q", "-m", "first")
    git_in(work, "checkout", "-q", "-b", "fix")
    (work / "a.js").write_text("fixed\n")
    git_in(work, "commit", "-qam", "the fix")
    git_in(work, "checkout", "-q", "master")
    git_in(work, "merge", "-q", "--no-ff", "fix", "-m", "Merge branch 'fix' into master")
    clean = git_in(work, "rev-parse", "HEAD")
    git_in(work, "checkout", "-q", "-b", "sync", "HEAD~1")
    (work / "b.js").write_text("other work\n")
    git_in(work, "add", ".")
    git_in(work, "commit", "-q", "-m", "other work on the branch")
    git_in(work, "merge", "-q", "--no-ff", "master", "-m", "Merge branch 'master' into sync")
    sync = git_in(work, "rev-parse", "HEAD")
    bare = tmp_path / "repos" / "github.com" / "o" / "r.git"
    bare.parent.mkdir(parents=True)
    git_in(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    return bare, clean, sync


def test_git_says_whether_the_tree_of_a_merge_is_the_tree_of_its_second_parent(merges):
    bare, clean, sync = merges
    assert codemine.tree_equal_of(bare, clean) is True, "master had nothing the fix branch did not: the merge is the fix branch"
    assert codemine.tree_equal_of(bare, sync) is False, "the branch had other work the merge keeps"
    assert codemine.tree_equal_of(bare, "0" * 40) is None, "an unknown commit is not decided"


def test_the_github_api_says_it_with_one_more_request_and_only_for_a_two_parent_commit():
    path, p2 = f"/repos/o/r/commits/{SHA}", "q" * 40
    body = {"sha": SHA, "parents": [{"sha": PARENT}, {"sha": p2}], "commit": {"message": "Merge branch 'dev'", "committer": {"date": "d"}, "tree": {"sha": "T" * 40}}, "files": [gh_file(1)]}
    same = Session({path: Resp(200, body), f"/repos/o/r/git/commits/{p2}": Resp(200, {"sha": p2, "tree": {"sha": "T" * 40}})})
    assert fetcher(same).commit({"owner": "o", "repo": "r", "sha": SHA}).tree_equal is True and len(same.calls) == 2
    other = Session({path: Resp(200, body), f"/repos/o/r/git/commits/{p2}": Resp(200, {"sha": p2, "tree": {"sha": "U" * 40}})})
    assert fetcher(other).commit({"owner": "o", "repo": "r", "sha": SHA}).tree_equal is False
    unknown = Session({path: Resp(200, body)})
    assert fetcher(unknown).commit({"owner": "o", "repo": "r", "sha": SHA}).tree_equal is None, "a failed lookup decides nothing"
    one = Session({path: Resp(200, {**body, "parents": [{"sha": PARENT}]})})
    assert fetcher(one).commit({"owner": "o", "repo": "r", "sha": SHA}).tree_equal is None and len(one.calls) == 1

