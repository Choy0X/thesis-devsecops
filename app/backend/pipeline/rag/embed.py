"""Text embeddings for the retrieval knowledge bases.

An :class:`Embedder` is **pinned**: model id, revision, output dimension and the instruction put in front of queries. The pin is stored with
every index (``kb_chunks__<slug>`` and its ``rag_index_state`` entry), a query is always embedded with the embedder that built the index,
and an index whose embedder is no longer the configured one is reported stale and refuses queries until it is rebuilt (vectors of different
models are not comparable). A different embedder gets its own collection and vector index; nothing is dropped silently.

Where the vectors come from (the embedding engine in Settings):
* **Ollama** (``kind="ollama"``): an installed embedding model selected by tag and pinned by the digest Ollama reports.
* **local** (``kind="gguf"``): a GGUF model (``ZTSA_EMBED_LOCAL_MODEL``, or the one embedding model found in the models folder), run by llama.cpp behind the model gateway
  (``embed_client.local_vectors``). The gateway decides where it runs and shares the graphics card with the language model; this module knows nothing of that. Its pin is the file's
  name, the start of its SHA-256 as the revision and its quantisation, so replacing the file marks every index made with it stale. ``RAG_DIMENSIONS`` may cut the vector to fewer values
  than the model's own (the models used here are trained for it).
* **external** (``kind="api"``): an OpenAI-compatible embeddings endpoint (``EMBED_BASE_URL``, ``EMBED_API_KEY``, ``EMBED_MODEL``); ``RAG_DIMENSIONS`` is the size it returns.
The recommended local model is ``jinaai/jina-code-embeddings-0.5b-GGUF`` (``RECOMMENDED``); it was checked against the sentence-transformers model it replaced (cosine 0.9996, the
same retrieval).

Vectors are L2-normalised here, so cosine similarity in the index is meaningful whatever the backend returns.
"""
from __future__ import annotations

import functools
import hashlib
import math
import os
import pathlib
import re
from dataclasses import dataclass

from app.backend.pipeline.rag import embed_client

BATCH = 64                                                         # chunks embedded and stored at a time (the client sends them on in batches of its own)
RECOMMENDED = {"repo": "jinaai/jina-code-embeddings-0.5b-GGUF", "file": "jina-code-embeddings-0.5b-Q8_0.gguf", "size_mb": 531, "dimension": 896,
               "licence": "CC-BY-NC-4.0: for research, not for commercial use"}
DEFAULT_QUERY_INSTRUCTION = "Find the most relevant code snippet given the following query:\n"        # the card's nl2code task
DEFAULT_DOCUMENT_INSTRUCTION = "Candidate code snippet:\n"
QWEN3_QUERY_INSTRUCTION = "Instruct: Given a piece of code or a vulnerability description, retrieve similar vulnerable code and advisories\nQuery:"


@dataclass(frozen=True)
class Embedder:
    model: str
    dimension: int
    revision: str = "unpinned"             # the start of the file's SHA-256 for a GGUF model, "api" for an endpoint
    kind: str = "gguf"                     # gguf | ollama | api | test
    base_url: str = ""
    query_instruction: str = ""
    document_instruction: str = ""
    quantisation: str = ""                 # a GGUF model's quantisation (Q8_0 ...): other quantisations give other vectors

    @property
    def engine(self) -> str:
        return {"gguf": "local", "api": "external", "test": "test"}.get(self.kind, "local")

    @property
    def instruction_tag(self) -> str:
        """Empty without instructions; otherwise a short hash of them: other instructions give other vectors, so they are part of the pin."""
        if not (self.query_instruction or self.document_instruction):
            return ""
        return hashlib.sha256(f"{self.query_instruction}\x00{self.document_instruction}".encode()).hexdigest()[:6]

    @property
    def key(self) -> str:
        tag = self.instruction_tag
        return f"{self.model}@{self.revision}/{self.dimension}" + (f"+i{tag}" if tag else "")

    @property
    def slug(self) -> str:
        """Collection-name suffix: readable model name, dimension, the start of the revision and the instructions' tag (lowercase letters, digits, underscores)."""
        name = re.sub(r"[^a-z0-9]+", "_", self.model.lower()).strip("_")[-40:]
        rev = re.sub(r"[^a-z0-9]", "", self.revision.lower())[:7]
        return "_".join(p for p in (name, str(self.dimension), rev, ("i" + self.instruction_tag) if self.instruction_tag else "") if p)

    def as_dict(self) -> dict:
        return {"model": self.model, "revision": self.revision, "dimension": self.dimension, "kind": self.kind, "engine": self.engine, "quantisation": self.quantisation,
                "base_url": self.base_url, "query_instruction": self.query_instruction, "document_instruction": self.document_instruction, "key": self.key}

    @classmethod
    def from_dict(cls, d: dict) -> "Embedder":
        return cls(model=d["model"], dimension=int(d["dimension"]), revision=d.get("revision", "unpinned"), kind=d.get("kind", "gguf"),
                   base_url=d.get("base_url", ""), query_instruction=d.get("query_instruction", ""), document_instruction=d.get("document_instruction", ""),
                   quantisation=d.get("quantisation", ""))

    # -- vectors ------------------------------------------------------------------------------------------------------------------
    def encode_documents(self, texts: list[str]) -> list[list[float]]:
        return [normalise(v) for v in self._encode(texts, query=False)]

    def encode_query(self, text: str) -> list[float]:
        return normalise(self._encode([text], query=True)[0])

    def _encode(self, texts: list[str], *, query: bool) -> list[list[float]]:
        if self.kind == "test":                      # an embedder rebuilt from its stored pin (``from_dict``) is a plain Embedder
            return HashEmbedder(dimension=self.dimension)._encode(texts, query=query)
        if self.kind == "local":                     # the pin of an index made when the model ran inside the application
            raise embed_client.EmbedError(f"This index was made with {self.model} running inside the application, which the app no longer does. Build it again with the embedding model chosen under Retrieval.")
        texts = [self._prefixed(t, query) for t in texts]
        if self.kind == "ollama":
            return embed_client.ollama_vectors(texts, model=self.model, revision=self.revision, dimension=self.dimension)
        if self.kind == "gguf":
            return embed_client.local_vectors(texts, self.dimension, priority="chat" if query else "batch")
        if self.kind == "api":
            return embed_client.endpoint_vectors(texts, model=self.model, dimension=self.dimension, base_url=os.getenv("EMBED_BASE_URL") or self.base_url)
        raise embed_client.EmbedError(f"Unknown kind of embedder: {self.kind}.")

    def _prefixed(self, text: str, query: bool) -> str:
        instruction = self.query_instruction if query else self.document_instruction
        return f"{instruction}{text}" if instruction else text


def normalise(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else list(v)


def enabled() -> bool:
    """Whether retrieval for the project index and the chat is on (the Settings switch; on unless it says ``off``)."""
    return (os.getenv("RAG_ENABLED") or "").strip().lower() != "off"


def default_instructions(model: str) -> tuple[str, str]:
    """(query, document) instruction for the models whose cards define one; empty for the others."""
    low = model.lower()
    if "jina-code-embeddings" in low:
        return DEFAULT_QUERY_INSTRUCTION, DEFAULT_DOCUMENT_INSTRUCTION
    if "qwen3-embedding" in low:
        return QWEN3_QUERY_INSTRUCTION, ""
    return "", ""


def engine() -> str:
    """The embedding engine chosen in Settings; with none chosen, an embeddings endpoint that is saved means external and nothing saved means local."""
    if ollama_model_tag():
        return "local"
    chosen = (os.getenv("EMBED_ENGINE") or "").strip().lower()
    if chosen in ("local", "external"):
        return chosen
    return "external" if os.getenv("EMBED_BASE_URL") else "local"


def ollama_model_tag() -> str:
    return (os.getenv("ZTSA_EMBED_OLLAMA_MODEL") or "").strip()


def ollama_model(tag: str) -> dict:
    from app.backend.pipeline.ollama import catalog, client

    try:
        selected = catalog.local_embedding(tag)
    except client.OllamaError as exc:
        raise ValueError(f"Could not read installed Ollama models. {exc}") from exc
    if selected is None:
        raise ValueError(f"Ollama embedding model {tag} is unavailable. Choose an installed model in Settings.")
    return selected


@functools.lru_cache(maxsize=16)
def _probe_ollama_width(tag: str, revision: str) -> int:
    try:
        return len(embed_client.ollama_vectors(["embedding dimension"], model=tag, revision=revision, dimension=None)[0])
    except embed_client.EmbedError as exc:
        raise ValueError(str(exc)) from exc


@functools.lru_cache(maxsize=8)
def _file_sha(path: str, size: int, mtime_ns: int) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(1 << 22):
            h.update(block)
    return h.hexdigest()


def file_revision(path) -> str:
    """The start of the SHA-256 of a model file: the revision of a GGUF model (the file is read once per size and time)."""
    p = pathlib.Path(path)
    st = p.stat()
    return _file_sha(str(p), st.st_size, st.st_mtime_ns)[:12]


def local_model_path() -> str:
    """The GGUF file of the local embedding model: the one chosen in Settings, else the one embedding model in the models folder; "" when there is none."""
    chosen = (os.getenv("ZTSA_EMBED_LOCAL_MODEL") or "").strip()
    if chosen:
        return chosen
    from app.backend.pipeline import model_files
    found = model_files.find_embedding_gguf()
    return str(found) if found else ""


def gguf_embedder(path) -> Embedder:
    """The embedder for a GGUF model file: pinned by name, the start of its SHA-256 and its quantisation, sized by ``RAG_DIMENSIONS`` or by the model's own width."""
    from app.backend.pipeline import gguf
    p = pathlib.Path(path)
    if not p.is_file():
        raise ValueError(f"The embedding model file does not exist: {p.name}. Choose another under Retrieval in Settings.")
    try:
        info = gguf.describe(p)
    except gguf.GgufError as e:
        raise ValueError(str(e)) from e
    width = int(info.get("hidden") or 0)
    dim = int(os.getenv("RAG_DIMENSIONS") or width or 0)
    if not dim:
        raise ValueError(f"The vector size of {p.name} is not known. Set it under Retrieval in Settings.")
    if width and dim > width:
        raise ValueError(f"{p.name} makes vectors of {width} values; the size {dim} in Settings is larger. Choose {width} or less.")
    q, d = default_instructions(p.name)
    return Embedder(model=p.name, dimension=dim, revision=file_revision(p), kind="gguf", query_instruction=q, document_instruction=d, quantisation=info.get("quantisation") or "")


NOT_CHOSEN = ("No embedding model is chosen. Choose one under Retrieval in Settings, or download the recommended one (" + RECOMMENDED["repo"] + ") in the Model Hub.")


def configured() -> Embedder:
    """Resolve the configured embedder and pin its identity and vector size."""
    if os.getenv("RAG_LOCAL_MODEL") == "hash-bow":            # offline development and checks: no model, no network (see HashEmbedder); not a setting
        return HashEmbedder(dimension=int(os.getenv("RAG_DIMENSIONS") or 32))
    tag = ollama_model_tag()
    if tag:
        selected = ollama_model(tag)
        tag, revision = selected["tag"], selected["digest"]
        dimension = selected["embedding_length"] or _probe_ollama_width(tag, revision)
        q, d = default_instructions(f"{tag} {selected['name']}")
        return Embedder(model=tag, dimension=dimension, revision=revision, kind="ollama", query_instruction=q, document_instruction=d)
    if engine() == "external":
        from app.backend.config import normalize_base_url
        base, model = normalize_base_url(os.getenv("EMBED_BASE_URL") or ""), os.getenv("EMBED_MODEL") or ""
        if not (base and model):
            raise ValueError("The external embedding endpoint and model are not set. Set them under Retrieval in Settings.")
        dim = int(os.getenv("RAG_DIMENSIONS") or 0)
        if not dim:
            raise ValueError(f"The vector size of {model} is not known yet. Test the model in Settings, which finds it out and saves it.")
        q, d = default_instructions(model)
        return Embedder(model=model, dimension=dim, revision="api", kind="api", base_url=base, query_instruction=q, document_instruction=d)
    path = local_model_path()
    if not path:
        raise ValueError(NOT_CHOSEN)
    return gguf_embedder(path)


@dataclass(frozen=True)
class HashEmbedder(Embedder):
    """A deterministic bag-of-words embedder for tests and offline checks: words are hashed into ``dimension`` buckets, so texts that share
    words get similar vectors. No model, no network."""
    kind: str = "test"
    model: str = "hash-bow"
    revision: str = "1"
    dimension: int = 32
    query_instruction: str = ""
    document_instruction: str = ""

    def _encode(self, texts: list[str], *, query: bool) -> list[list[float]]:
        out = []
        for t in texts:
            v = [0.0] * self.dimension
            for w in re.findall(r"[A-Za-z0-9_]+", t.lower()):
                h = int.from_bytes(hashlib.blake2b(w.encode(), digest_size=4).digest(), "big")
                v[h % self.dimension] += 1.0 if (h >> 31) & 1 else -1.0
            out.append(v)
        return out


# -- compatibility with the callers of the old module -----------------------------------------------------------------------------

def current() -> dict:
    """The configured embedder as a plain dict (recorded in run_config.json)."""
    e = configured()
    return e.as_dict()


def encode(texts: list[str], spec: dict | None = None) -> list[list[float]]:
    e = Embedder.from_dict(spec) if spec else configured()
    return e.encode_documents(texts)
