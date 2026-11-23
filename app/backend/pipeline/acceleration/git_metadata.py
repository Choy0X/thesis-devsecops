"""Git-first metadata from exact immutable commits, with explicit provenance.

Raw paths and statistics use NUL framing. Source blobs are fetched explicitly
before diff generation; no Git diff command may start an implicit network fetch.
"""
from dataclasses import dataclass
import datetime
import hashlib
import os
import re

from .repository_service import CompatibilityFallback, service
from . import operations
from .metrics import current

_HEADER = re.compile(rb":([0-7]{6}) ([0-7]{6}) ([0-9a-f]{40}) ([0-9a-f]{40}) ([AMDRT])([0-9]*)\Z")
MAX_METADATA_BLOB = 1 << 20
MAX_METADATA_BYTES = 16 << 20


@dataclass
class RawChange:
    path: str
    old_path: str
    status: str
    blob_sha: str
    old_sha: str
    old_mode: str
    new_mode: str


def _path(raw):
    try:
        path = raw.decode("utf-8")
    except UnicodeError as exc:
        raise CompatibilityFallback("Git path is not UTF-8") from exc
    if not path:
        raise CompatibilityFallback("Git path is empty")
    return path


def parse_raw(raw):
    """Parse --raw -z --no-abbrev output without splitting path characters."""
    if not raw:
        return []
    if not raw.endswith(b"\0"):
        raise CompatibilityFallback("Truncated NUL-framed Git raw diff")
    tokens = raw.split(b"\0")[:-1]
    rows, offset = [], 0
    while offset < len(tokens):
        match = _HEADER.fullmatch(tokens[offset])
        if match is None or offset + 1 >= len(tokens):
            raise CompatibilityFallback("Unsupported Git raw diff record")
        old_mode, new_mode, old_sha, new_sha, code, score = match.groups()
        offset += 1
        old_path = path = _path(tokens[offset]); offset += 1
        if code == b"R":
            if not score or int(score) > 100 or offset >= len(tokens):
                raise CompatibilityFallback("Truncated Git rename record")
            path = _path(tokens[offset]); offset += 1
        elif score:
            raise CompatibilityFallback("Unexpected Git similarity score")
        status = {b"A": "added", b"D": "removed", b"R": "renamed"}.get(code, "modified")
        rows.append(RawChange(path, old_path, status, new_sha.decode("ascii"), old_sha.decode("ascii"),
                              old_mode.decode("ascii"), new_mode.decode("ascii")))
    return rows


def _stats(raw):
    tokens, offset, result = raw.split(b"\0"), 0, {}
    while offset < len(tokens) - 1:
        record = tokens[offset]; offset += 1
        fields = record.split(b"\t", 2)
        if len(fields) != 3:
            raise CompatibilityFallback("Invalid Git numstat record")
        additions, deletions, name = fields
        if name:
            old_path = path = _path(name)
        else:
            if offset + 1 >= len(tokens) - 1:
                raise CompatibilityFallback("Truncated Git rename numstat")
            old_path, path = _path(tokens[offset]), _path(tokens[offset + 1]); offset += 2
        if additions == deletions == b"-":
            counts = (0, 0)
        elif additions.isdigit() and deletions.isdigit():
            counts = (int(additions), int(deletions))
        else:
            raise CompatibilityFallback("Unsupported Git line statistics")
        result[(old_path, path)] = counts
    if tokens[-1] != b"":
        raise CompatibilityFallback("Truncated NUL-framed Git numstat")
    return result



def parse_combined(raw):
    """Split raw paths, NUL-framed numstat and exact patch bytes from one Git diff.

    Consume records by their documented arity rather than searching for a patch
    marker that could occur inside a filename. Existing parsers remain the
    authority for path/status/statistics validation.
    """
    if not raw:
        return [], {}, b""
    offset = 0
    def token():
        nonlocal offset
        end = raw.find(b"\0", offset)
        if end < 0:
            raise CompatibilityFallback("Truncated combined Git diff record")
        value = raw[offset:end]
        offset = end + 1
        return value
    while raw[offset:offset + 1] == b":":
        match = _HEADER.fullmatch(token())
        if match is None:
            raise CompatibilityFallback("Unsupported combined Git raw diff")
        token()
        if match[5] == b"R":
            token()
    changes = parse_raw(raw[:offset])
    if not changes:
        raise CompatibilityFallback("Combined Git diff has no raw inventory")
    stats_start = offset
    for change in changes:
        fields = token().split(b"\t", 2)
        if len(fields) != 3:
            raise CompatibilityFallback("Invalid combined Git numstat")
        if not fields[2]:
            token(); token()
    stats = _stats(raw[stats_start:offset])
    if raw[offset:offset + 1] != b"\0":
        raise CompatibilityFallback("Combined Git sections have no exact separator")
    return changes, stats, raw[offset + 1:]


def _patches(raw, changes):
    if not raw:
        if changes: raise CompatibilityFallback("Git patch inventory is empty")
        return []
    chunks = re.split(rb"(?m)^diff --git ", raw)
    if chunks[0] or len(chunks) - 1 != len(changes):
        raise CompatibilityFallback("Git patch inventory does not match raw paths")
    patches = []
    for chunk in chunks[1:]:
        found = re.search(rb"(?m)^@@ ", chunk)
        if found is None:
            patches.append(None)  # binary, unchanged rename, or mode-only change
        else:
            try: patches.append(chunk[found.start():].decode("utf-8").removesuffix("\n"))
            except UnicodeError as exc:
                raise CompatibilityFallback("Git patch is not UTF-8") from exc
    return patches


def _diff(path, metadata, *options):
    from app.backend.pipeline.ingest import codemine as cm
    if os.environ.get('ZTSA_EXPERIMENTAL_PERSISTENT_DIFF') == '1':
        from .diff_reader import PRESETS
        preset = next((name for name, value in PRESETS.items() if value == options), None)
        if preset is not None:
            try:
                return service(path).diff(metadata, preset)
            except operations.DeadlineExpired:
                raise
            except (OSError, CompatibilityFallback) as error:
                operations.check()
                current.count('git_persistent_diff_reference_fallbacks')
                print('Persistent Git diff requires reference acquisition: ' + str(error)[:300])
    revisions = [metadata["parent"], metadata["sha"]] if metadata["parent"] else [metadata["sha"]]
    return cm._git(["-C", str(path), "-c", "diff.algorithm=myers", "-c", "diff.indentHeuristic=true",
                    "-c", "core.quotePath=true", "diff-tree", "--root", "-r", "--no-commit-id", "--no-ext-diff",
                    "--no-textconv", "--no-abbrev", "--full-index", *options, *revisions], text=False, no_lazy=True)


def _hydrate(path, metadata):
    """Plan only changed objects before rename detection needs their contents."""
    from .bulk_git import Repository
    changes = parse_raw(_diff(path, metadata, "--raw", "-z", "--no-renames"))
    oids = []
    for row in changes:
        if row.old_mode not in ("000000", "100644", "100755") or row.new_mode not in ("000000", "100644", "100755"):
            raise CompatibilityFallback("Special Git file mode requires reference acquisition")
        from app.backend.pipeline.security_snapshots import logical_path
        try: logical_path(row.old_path); logical_path(row.path)
        except ValueError as exc: raise CompatibilityFallback("Git path requires reference acquisition") from exc
        before=row.status!='added'
        after=row.status!='removed'
        if (before and not metadata['parent'] or
            (row.old_mode!='000000')!=before or (row.old_sha!='0'*40)!=before or
            (row.new_mode!='000000')!=after or (row.blob_sha!='0'*40)!=after):
            raise CompatibilityFallback('Raw Git changed-object binding is inconsistent')
        if before:oids.append(row.old_sha)
        if after:oids.append(row.blob_sha)
    repository = Repository(path)
    # _diff is local-only and names both exact revisions. Its full raw OIDs
    # already bind these paths to their snapshots; no flattened path index is
    # needed here. acquire() still proves complete raw snapshot trees first.
    oids = list(dict.fromkeys(oids))
    shared = service(path)
    sizes = shared.info_many(oids)
    # Missing blobs have no size yet. Explicit batch hydration below fetches them;
    # enforce the bound again before running the content-consuming diff commands.
    if any(info and info[2] > MAX_METADATA_BLOB for info in sizes):
        raise CompatibilityFallback("Changed Git blob exceeds metadata budget")
    repository.prefetch_oids(oids)
    sizes = shared.info_many(oids)
    if any(info and info[1] == "blob" and info[2] > MAX_METADATA_BLOB for info in sizes):
        raise CompatibilityFallback("Changed Git blob exceeds metadata budget")
    if any(not info or info[1] != "blob" or info[2] > MAX_METADATA_BLOB for info in sizes) or sum(info[2] for info in sizes if info) > MAX_METADATA_BYTES:
        raise CompatibilityFallback("Changed Git objects exceed bounded metadata acquisition")


def acquire(row, git, github=None):
    """Acquire the exact local snapshot; unusual interpretation goes to fallback."""
    from app.backend.pipeline.ingest import codemine as cm
    path = git._repo(row)
    with cm._repo_lock(path):
        shared = service(path)
        sha = row["sha"]
        from . import mining_policy
        snapshot_policy=mining_policy.current().get('version')==4
        shared._exact(sha,allow_shallow=snapshot_policy)
        if shared.info_many([sha])[0] is None:
            cm._git(["-C", str(path), "-c", "protocol.version=2", "fetch", "-q", "--depth=2",
                     "--filter=blob:none", "--no-tags", "--no-write-fetch-head", "origin", sha], timeout=240)
            shared.invalidate_availability()
        metadata = shared.snapshot_metadata(sha,owned_root=cm.REPOS) if snapshot_policy else shared.metadata(sha)
        parents = metadata["parents"]
        if parents and any(info is None for info in shared.info_many(parents)):
            raise CompatibilityFallback("Exact Git parent is unavailable locally")
        if "\ufffd" in metadata["message"]:
            raise CompatibilityFallback("Commit message needs reference encoding interpretation")
    _hydrate(path, metadata)
    combined = _diff(path, metadata, "--raw", "--numstat", "-p", "-z", "-M",
                     "--unified=3", "--src-prefix=a/", "--dst-prefix=b/")
    changes, stats, patch_bytes = parse_combined(combined)
    patches = _patches(patch_bytes, changes)
    files = []
    for change, patch in zip(changes, patches):
        counts = stats.get((change.old_path, change.path))
        if counts is None: raise CompatibilityFallback("Git raw/numstat inventories disagree")
        files.append(cm.FileChange(change.path, change.old_path, change.status, patch, *counts,
                                   change.old_sha if change.status == "removed" else change.blob_sha))
    if len(stats) != len(files): raise CompatibilityFallback("Git statistics include unmatched paths")
    tree_equal = None
    if len(parents) == 2:
        other = shared._object(parents[1], 1 << 20)
        if other is None or other[1] != "commit": raise CompatibilityFallback("Merge parent object unavailable")
        parent_tree = re.search(rb"(?:^|\n)tree ([0-9a-f]{40})(?:\n|$)", other[3].partition(b"\n\n")[0])
        if parent_tree is None: raise CompatibilityFallback("Merge parent tree is unavailable")
        tree_equal = metadata["tree"] == parent_tree[1].decode("ascii")
    date = datetime.datetime.fromisoformat(metadata["date"]).astimezone(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    profile_loader = cm._GitLoader(path)
    profile_source = "git-snapshot-paths"
    if github is not None and getattr(github, "s", None) is not None:
        # The existing GitHub path chooses languages using repository byte shares.
        # Keep that cached per-repository input while eliminating commit REST.
        profile_loader = cm._RawLoader(github.s, row["owner"], row["repo"])
        profile_source = "github-repository-languages"
    loader = cm._BatchedGitLoader(path, profile_loader)
    commit = cm.Commit(sha, metadata["parent"], metadata["message"].removesuffix("\n"), date, files,
                       loader, list(parents), tree_equal)
    commit.acquisition = {"schema": "mining-acquisition/1", "transport": "git-snapshot", "commit": sha,
                          "parent": metadata["parent"], "parents": list(parents), "tree": metadata["tree"], "profile_source": profile_source,
                          "diff": {"algorithm": "myers", "renames": "50%", "context": 3, "index_format": "full", "patch_sha256": hashlib.sha256(patch_bytes).hexdigest()}}
    current.count("git_snapshot_metadata_commits")
    operations.check()
    return commit
