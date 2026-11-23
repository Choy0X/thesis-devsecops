"""The embedded document model: collections, validators, indexes and search definitions, in one place.

``database.ensure_schema`` applies these definitions to local SQLite idempotently. The retained document and search metadata also describe
historical MongoDB exports; runtime text search uses FTS5 and vector search uses exact local scoring.

Conventions
- snake_case names; ``*_at`` fields are typed dates in UTC; booleans are ``is_*`` / ``has_*``; enums are short lowercase strings.
- Absent means null: nothing is stored as null. Delimited strings and JSON text of the old model are arrays and sub-documents.
- ``_id`` is an integer from the ``counters`` collection for the entities the API addresses by number (advisories, fix commits, code pairs,
  review decisions, leakage edges) and a natural string for small keyed entities.
- Wide text never sits in a document that is listed, grouped or gated: code lives in ``pair_code``, LLM drafts in ``pair_enrichments``.
- Every sub-document has one owning writer stage (named in the comments), so a stage never rewrites another stage's data.
"""
from __future__ import annotations

from dataclasses import dataclass, field

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Index:
    keys: tuple                        # ((field, 1 or -1), ...)
    name: str
    unique: bool = False
    partial: dict | None = None        # partialFilterExpression
    ttl_seconds: int | None = None

    def options(self) -> dict:
        out = {"name": self.name}
        if self.unique:
            out["unique"] = True
        if self.partial:
            out["partialFilterExpression"] = self.partial
        if self.ttl_seconds is not None:
            out["expireAfterSeconds"] = self.ttl_seconds
        return out


@dataclass(frozen=True)
class SearchIndex:
    name: str
    definition: dict
    type: str = "search"               # retained definition types: "search" or "vectorSearch"


@dataclass(frozen=True)
class Collection:
    name: str
    doc: str
    validator: dict = field(default_factory=dict)          # document $jsonSchema; retained BSON type names are interpreted locally
    indexes: tuple = ()
    search_indexes: tuple = ()
    zstd: bool = False                 # historical storage hint, retained for migration compatibility


def ix(*keys, name: str | None = None, unique: bool = False, partial: dict | None = None, ttl: int | None = None) -> Index:
    """``ix("a", ("b", -1))`` -> an index on a ascending, b descending, named after its keys."""
    norm = tuple((k, 1) if isinstance(k, str) else (k[0], k[1]) for k in keys)
    auto = "_".join(f"{k}_{d}" for k, d in norm)
    return Index(norm, name or auto, unique, partial, ttl)


def schema(required: list[str], **props) -> dict:
    """A small $jsonSchema: required fields and the types of the listed properties (other fields are allowed)."""
    body = {"bsonType": "object", "properties": props}
    if required:
        body["required"] = required                    # omit an empty array for historical schema compatibility
    return {"$jsonSchema": body}


STR, INT, BOOL, DATE, ARR, OBJ, DBL = "string", "long", "bool", "date", "array", "object", "double"
NUM = ["int", "long"]
STR_NULL = ["string", "null"]
ENUM_KIND = {"enum": ["vulnerability", "malware", "withdrawn"]}

COLLECTIONS: tuple[Collection, ...] = (
    # ---- advisory domain ----------------------------------------------------------------------------------------------------------
    Collection(
        "advisories",
        "One OSV advisory per ecosystem (was ``records``). ingest owns the identity and advisory facts, depversions owns ``deps``, the "
        "miner's roll-up owns ``mining``, link recovery owns ``link_recovery``. The advisory's packages and affected ranges are embedded "
        "(was ``advisory_packages`` and the ``affected`` JSON).",
        schema(["key", "kind", "created_at"], key={"bsonType": STR}, kind=ENUM_KIND,
               aliases={"bsonType": ARR}, cwe={"bsonType": ARR}, packages={"bsonType": ARR}, refs={"bsonType": ARR}),
        indexes=(ix("key", unique=True), ix("osv_id"), ix("cve_id"), ix("aliases"),
                 ix("kind", "ecosystem", "mining.code_status", name="kind_eco_code_status"),
                 ix("mining.code_status", ("_id", -1), name="code_status_newest"),
                 ix("deps.status", ("_id", -1), name="deps_status_newest"),
                 ix("mining.languages", ("_id", -1), name="languages_newest"),
                 ix("published_at", ("_id", -1), name="published_newest"),
                 ix("ecosystem", ("_id", -1), name="ecosystem_newest"),
                 ix("osv_severity", ("_id", -1), name="severity_newest"),
                 ix("packages.ecosystem", "packages.name", name="package_lookup"),
                 ix("kind", "mining.code_status", "deps.checked_at", name="deps_queue")),
        search_indexes=(SearchIndex("advisories_search", {"mappings": {"dynamic": False, "fields": {
            "osv_id": {"type": "string", "analyzer": "lucene.keyword"},
            "cve_id": {"type": "string", "analyzer": "lucene.keyword"},
            "aliases": {"type": "string", "analyzer": "lucene.keyword"},
            "summary": {"type": "string"},
            "packages": {"type": "document", "fields": {
                "name": [{"type": "string", "analyzer": "lucene.keyword"}, {"type": "autocomplete", "tokenization": "edgeGram", "minGrams": 2, "maxGrams": 15}]}},
            "kind": {"type": "token"}, "ecosystem": {"type": "token"},
            "mining": {"type": "document", "fields": {"code_status": {"type": "token"}, "languages": {"type": "token"}}},
            "deps": {"type": "document", "fields": {"status": {"type": "token"}}},
            "seq": {"type": "number"}}}}),),
    ),
    Collection(
        "fix_links",
        "The many-to-many link between an advisory and a fix commit (was ``records.fix_commits`` plus ``fix_commits.advisory_ids / cve_ids / "
        "sources / via``). Owned by link extraction and recovery.",
        schema(["advisory_id", "commit_key"], advisory_id={"bsonType": NUM}, commit_key={"bsonType": STR}, via={"bsonType": ARR},
               sources={"bsonType": ARR}),
        indexes=(ix("advisory_id", "commit_key", unique=True), ix("commit_key"), ix("advisory_key")),
    ),
    Collection(
        "fix_commits",
        "One fix commit. ``queue`` is owned by the miner (with the ecosystems of the records that point to the commit, and the campaign that "
        "queued or mined it), ``git`` by the miner's fetch, ``verification`` by the release-date check, ``evidence`` by link evidence.",
        schema(["commit_key"], commit_key={"bsonType": STR}),
        indexes=(ix("commit_key", unique=True), ix("queue.status", "_id", name="queue_fifo"),
                 ix("verification.fix_check", "queue.status", name="fix_check_status"), ix("evidence.level", "queue.status", name="evidence_status"),
                 ix("repo.identity"), ix("queue.ecosystems", "queue.status", "_id", name="queue_scope"),
                 ix("queue.campaign_id", name="queue_campaign", partial={"queue.campaign_id": {"$exists": True}})),
    ),
    Collection("repositories", "Licence of a repository, keyed by its lowercase host/owner/name identity.", schema([]), indexes=()),
    Collection("commit_files", "The files of a mined commit (was ``commit_files``; patches only for config and manifest files).",
               schema(["commit_key", "path"]), indexes=(ix("commit_key", "path", unique=True),)),
    Collection("registry_packages", "Registry versions and release dates of a package (was ``package_versions``), canonical ecosystem case.",
               schema(["ecosystem", "name"]), indexes=(ix("ecosystem", "name", unique=True),)),
    # ---- code domain --------------------------------------------------------------------------------------------------------------
    Collection(
        "code_pairs",
        "The light facts of a vulnerable/fixed code pair (about 1 KB). The code is in ``pair_code``, LLM drafts in ``pair_enrichments``. "
        "``link`` is a snapshot of the commit's verification written with the commit, for the review gate.",
        schema(["pair_key", "created_at"], pair_key={"bsonType": STR}, record_id={"bsonType": STR},
               origin={"bsonType": OBJ}, unit={"bsonType": OBJ}, flags={"bsonType": OBJ}),
        indexes=(ix("pair_key", unique=True), ix("record_id"), ix("commit_key", "flags.available", name="commit_available"),
                 ix("subject.cve_id"), ix("subject.advisory_id"),
                 ix("unit.language", "unit.granularity", "flags.available", ("_id", -1), name="list_filters"),
                 ix("link.fix_check", "link.evidence_level", "subject.ecosystem", ("attribution.basis", 1), name="review_gate",
                    partial={"flags.available": True}),
                 ix("extraction.content_hash"),
                 ix("flags.available", "flags.is_test", "flags.tangled", "enriched", "unit.granularity", "_id", name="enrichment_queue"),
                 ix("subject.cve_id", name="manual_by_cve", partial={"origin.kind": "manual"})),
        search_indexes=(SearchIndex("pairs_search", {"mappings": {"dynamic": False, "fields": {
            "subject": {"type": "document", "fields": {
                "cve_id": {"type": "string", "analyzer": "lucene.keyword"}, "package": {"type": "string", "analyzer": "lucene.keyword"},
                "cwe": {"type": "string", "analyzer": "lucene.keyword"}, "ecosystem": {"type": "token"}}},
            "unit": {"type": "document", "fields": {
                "file": {"type": "string", "analyzer": "lucene.keyword"}, "function": {"type": "string", "analyzer": "lucene.keyword"},
                "language": {"type": "token"}, "granularity": {"type": "token"}}},
            "repo_url": {"type": "string", "analyzer": "lucene.keyword"},
            "flags": {"type": "document", "fields": {"available": {"type": "boolean"}}},
            "seq": {"type": "number"}}}}),),
    ),
    Collection("pair_code", "The code of a pair: before, after and the diff (``_id`` = the pair's id). zstd block compression.",
               schema([]), zstd=True),
    Collection("pair_enrichments", "Model-drafted descriptions of a pair, always marked synthetic (``_id`` = the pair's id).", schema([])),
    Collection("pair_quality", "What the attribution found about a pair: the version of the rules, the content hash it was computed from, the change key (a hash of what the diff changes) and the "
               "signals (``_id`` = the pair's id). Kept apart because re-mining or re-splitting a pair replaces its whole document.", schema([]), indexes=(ix("pair_key", unique=True),)),
    # ---- review and derived -------------------------------------------------------------------------------------------------------
    Collection("review_decisions", "Append-only review decisions (the repository exposes insert and read only).",
               schema(["record_id", "reviewer", "review_status", "created_at"], record_id={"bsonType": STR}),
               indexes=(ix("record_id", ("_id", -1), name="record_latest"),)),
    Collection("review_current", "The latest decision per record, written in the same transaction as the decision (``_id`` = record id).",
               schema([])),
    Collection("review_exchange_receipts", "Append-only idempotence and provenance receipts for imported external review decisions.",
               schema(["decision_id", "packet_id", "source_sha256", "digest", "complete"],
                      decision_id={"bsonType": NUM}, packet_id={"bsonType": STR}, source_sha256={"bsonType": STR},
                      digest={"bsonType": STR}, complete={"bsonType": BOOL}),
               indexes=(ix("decision_id"), ix("pair_id"))),
    Collection("audits", "A drawn audit sample: one document with its items.", schema([])),
    Collection("leakage_graphs", "One header per leakage graph version (params, stats, oversized groups).", schema([])),
    Collection("leakage_edges", "Edges of the leakage graph.", schema(["graph_version", "src", "dst", "edge_type"]),
               indexes=(ix("graph_version", "src"), ix("graph_version", "dst"))),
    Collection("leakage_groups", "Group membership of the leakage graph.", schema(["graph_version", "node_id", "group_id"]),
               indexes=(ix("graph_version", "node_id", unique=True), ix("graph_version", "group_id"))),
    Collection("split_manifests", "Frozen split manifest headers (``_id`` = version).", schema([])),
    Collection("split_groups", "The groups of a split manifest (the inverse node map of the old body is not stored).",
               schema(["manifest_version", "group_id"]),
               indexes=(ix("manifest_version", "group_id", unique=True), ix("manifest_version", "members", name="member_lookup"))),
    Collection("campaigns", "A collection campaign: params, targets, per-stage progress and its lease. A campaign of the second system has a ``plan`` (what was chosen), "
               "a ``control`` flag the running process reads (run or pause), its job, process and heartbeat, and ``progress``, ``summary`` and ``declared`` (the resources it was admitted with).",
               schema([]), indexes=(ix(("created_at", -1)), ix("state"))),
    Collection("campaign_events", "The append-only story of a campaign (``_id`` an increasing integer): created, started, stage_started, stage_done, stage_error, "
               "pause_requested, paused, resumed, interrupted, forced_kill, rate_limited, warning.", schema(["campaign_id", "at", "kind"]), indexes=(ix("campaign_id", "_id"),)),
    Collection("campaign_samples", "A campaign's counters over time (cumulative commits and pairs, GitHub quota, queue, memory), at most one every 30 seconds.",
               schema(["campaign_id", "at"]), indexes=(ix("campaign_id", "at"),)),
    Collection("campaign_owners", "Which campaign works on which ecosystem (``_id`` = the ecosystem): the atomic claim that keeps two campaigns off one ecosystem.", schema(["campaign_id"]),
               indexes=(ix("campaign_id"),)),
    Collection("campaign_steps", "Progress of one stage of one target of a campaign (``_id`` = campaign|target|stage). A separate collection "
               "because target names contain dots (crates.io), which cannot be field names of a nested update.", schema(["campaign_id", "target", "stage", "status"]),
               indexes=(ix("campaign_id"),)),
    Collection("model_exposure", "Which leakage groups a model trained on under a split manifest (``_id`` = model|manifest|group).", schema([]),
               indexes=(ix("manifest_version", "model_id"),)),
    Collection("model_contamination", "Validation groups a new link ties to a group some model trained on (``_id`` = model|manifest|val|linked).",
               schema([]), indexes=(ix("manifest_version", "model_id"),)),
    Collection("context_bundles", "Model-input context bundles kept once by their hash (``_id`` = bundle hash).", schema([])),
    Collection("context_links", "The bundle each record points at (``_id`` = record id).", schema([])),
    Collection("snapshots", "Provenance of raw copies (hash, size, source).", schema([])),
    Collection("rag_index_state", "State of the knowledge base (``_id`` = kb: version, pinned embedder, counts, ready) and of each project "
               "index (``_id`` = project:<id>).", schema([])),
    Collection("rag_index_files", "What a project's index has seen of each file (``_id`` = <project id>:<path>: size, modification time, hash; no hash for a file left out), so that "
               "the files changed since can be told without reading them all.", schema([]), indexes=(ix("project_id"),)),
    Collection("project_symbols", "The structural summary of one file of a project (``_id`` = <project id>:<path>): its module declaration, imports, module-level constants, types "
               "and top-level functions, read without an embedder, alongside the vector index -- a project_graph(path) tool reads this, never guesses at it.", schema([]), indexes=(ix("project_id"),)),
    Collection("agent_sessions", "One agent session without its messages (``_id`` = the 12-digit session id): project, mode, status, settings, pending call, task list, grants, usage, parent "
               "(a subagent's session), and the counters the store keeps (``rev``, ``msg_seq``, ``msgs``, ``bytes``). Written only through ``agentic/engine/store.py``.",
               schema(["project_id", "mode"], project_id={"bsonType": "string"}, mode={"bsonType": "string"}, status={"bsonType": "string"}),
               indexes=(ix("project_id", ("updated_at", -1)), ix("parent.session", name="parent_session", partial={"parent.session": {"$exists": True}}))),
    Collection("agent_messages", "The transcript of an agent session, one document per message (``_id`` = <session id>:<sequence>); ``m`` is the message as the loop wrote it, ``mid`` the message's own id. "
               "(A call id is not unique in a session: a server may give the same id to the calls of different rounds.)",
               schema(["sid", "seq", "m"], sid={"bsonType": "string"}, seq={"bsonType": ["int", "long"]}, m={"bsonType": "object"}),
               indexes=(ix("sid", "seq", name="session_order", unique=True), ix("sid", "mid", name="session_message"), ix("pid", name="project"))),
    Collection("agent_runs", "One run of the agent engine (``_id`` = r_<12 hex>): the work on one instruction across its pauses for approval. Its state and stop reason, the budget it was given "
               "and what it has spent (rounds, tool calls, tokens, active seconds), and ``jseq``, the count of its journal records. Written only through ``agentic/engine/store.py``.",
               schema(["session_id", "state"], session_id={"bsonType": "string"}, state={"bsonType": "string"}),
               indexes=(ix("session_id", ("started_at", -1), name="session_runs"), ix("project_id", name="project"))),
    Collection("agent_journal", "The journal of a run, one document per record (``_id`` = <run id>:<place>): the intent of a tool call before it ran, its result after, the rounds and the notes. "
               "It is what lets a restart tell what was in flight.",
               schema(["run_id", "seq", "k"], run_id={"bsonType": "string"}, seq={"bsonType": ["int", "long"]}, k={"bsonType": "string"}),
               indexes=(ix("run_id", "seq", name="run_order", unique=True), ix("sid", name="session"), ix("pid", name="project"))),
    # ---- system ---------------------------------------------------------------------------------------------------------------------
    Collection("system", "Key/value state of the application: schema version, collection window, mining run (``_id`` = key).", schema([])),
    Collection("counters", "Integer id sequences (``_id`` = collection name).", schema([])),
    Collection("leases", "Single-writer leases: mining, campaign (owner, host, pid, expiry).", schema([])),
    Collection("change_counters", "One counter per logical store, bumped once per write batch; replaces the file-stat change token.", schema([])),
)

BY_NAME = {c.name: c for c in COLLECTIONS}


def vector_collection(prefix: str, embedder_slug: str) -> str:
    """Name of the chunk collection of one embedder: ``kb_chunks__<slug>``, ``project_chunks__<slug>`` or ``web_chunks__<slug>``."""
    assert prefix in ("kb_chunks", "project_chunks", "web_chunks"), prefix
    return f"{prefix}__{embedder_slug}"


def vector_index(dimensions: int, kind: str) -> SearchIndex:
    """Retained vector definition of a chunk collection; the embedded engine scores normalised vectors locally."""
    filters = {"kb_chunks": ("kb_version", "split", "kind", "source_id", "leak_group", "cwe", "ecosystem", "language"),
               "project_chunks": ("project_id", "path", "language", "file_hash"),
               "web_chunks": ("url_hash", "domain", "fetched_at")}[kind]
    fields = [{"type": "vector", "path": "embedding", "numDimensions": dimensions, "similarity": "cosine", "quantization": "scalar"}]
    fields += [{"type": "filter", "path": f} for f in filters]
    return SearchIndex(f"{kind}_vec", {"fields": fields}, type="vectorSearch")
