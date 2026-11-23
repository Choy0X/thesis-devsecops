"""Where embedding vectors come from when the embedder is not a model loaded into this process: the local GGUF model behind the gateway, or an external OpenAI-compatible endpoint.

Both are the same call (``embeddings.create``) through the OpenAI client of ``llm_client``, in batches, with the vectors checked against the dimension the index was pinned with:

* **Local** (``kind="gguf"``): the gateway loads the embedding model when the first batch arrives (and may unload the language model to make room; see ``pipeline/gateway``). A vector may
  be cut to a smaller dimension than the model's own (the models used here are trained for it); a vector that is shorter than the pin is an error.
* **External** (``kind="api"``): the endpoint, key and model of Settings. The dimension in the pin must be what the endpoint returns; a different size is an error that says so, because
  cutting the vector of a model that was not trained for it would give an index that quietly searches badly.

The key of an external endpoint is read from the environment when a batch is sent and is never part of a pin.
"""
from __future__ import annotations

import os

BATCH = 32
TIMEOUT = 600.0


class EmbedError(RuntimeError):
    """The vectors could not be made; the message is a sentence for the person and ``code`` is the gateway's code when it gave one."""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


def _explain(e: Exception, where: str) -> EmbedError:
    """A failed call as a sentence. The gateway answers in the OpenAI error shape with its own ``code`` (gpu_exclusive, busy, does_not_fit, llama_cpp_missing ...)."""
    from app.backend.pipeline.gateway.errors import GatewayError
    if isinstance(e, GatewayError):
        return EmbedError(e.message, e.code)
    body = getattr(e, "body", None)
    err = (body.get("error") if isinstance(body, dict) and isinstance(body.get("error"), dict) else body) if body else None
    if isinstance(err, dict) and err.get("message"):
        return EmbedError(str(err["message"]), err.get("code"))
    status = getattr(e, "status_code", None)
    if status is None:
        return EmbedError(f"{where} did not answer ({type(e).__name__}).")
    return EmbedError(f"{where} answered {status}: {str(e)[:200]}")


def _vectors(client, model: str, texts: list[str], dimension: int | None, *, truncate: bool, where: str) -> list[list[float]]:
    """The vectors of ``texts``. With ``dimension`` None any size is accepted (the Settings test uses that to find the size out)."""
    out: list[list[float]] = []
    for start in range(0, len(texts), BATCH):
        part = texts[start:start + BATCH]
        try:
            response = client.embeddings.create(model=model, input=part, encoding_format="float")
        except Exception as e:
            raise _explain(e, where) from e
        rows = sorted(response.data, key=lambda d: d.index)
        if len(rows) != len(part):
            raise EmbedError(f"{where} returned {len(rows)} vectors for {len(part)} texts.")
        for row in rows:
            v = list(row.embedding)
            if dimension is None:
                out.append(v)
                continue
            if len(v) < dimension or (len(v) > dimension and not truncate):
                raise EmbedError(f"{where} returned vectors of {len(v)} values, but the index is pinned to {dimension}. "
                                 + ("Choose a smaller size under Retrieval, or build the index again." if len(v) < dimension else "Set the size of the embedding model to what the endpoint returns."))
            out.append(v[:dimension])
    return out


def local_vectors(texts: list[str], dimension: int, *, priority: str = "batch") -> list[list[float]]:
    """Vectors from the local embedding model through the gateway. ``priority`` is ``chat`` for the question of a person who is waiting."""
    from app.backend.pipeline import llm_client
    from app.backend.pipeline.gateway import client as gateway
    if not texts:
        return []
    try:
        model = gateway.inference_model("embed")
    except Exception as e:
        raise _explain(e, "Ollama") from e
    with llm_client.local_client(timeout=TIMEOUT) as client:
        return _vectors(client, model, texts, dimension, truncate=True, where="The local embedding model")


def ollama_vectors(texts: list[str], *, model: str, revision: str, dimension: int | None) -> list[list[float]]:
    """Read vectors from an installed Ollama model while guarding the index's model pin."""
    from app.backend.pipeline.gateway import client as gateway
    from app.backend.pipeline.ollama import catalog, client as ollama

    if not texts:
        return []
    vectors = []
    try:
        with gateway.inference_session("an embedding request"):
            selected = catalog.local_embedding(model)
            if selected is None or selected["digest"] != revision:
                raise EmbedError(f"Ollama model {model} has changed or is unavailable. Rebuild the index with an installed embedding model.", "model_changed")
            for start in range(0, len(texts), BATCH):
                part = texts[start:start + BATCH]
                rows = ollama.embed(selected["tag"], part, timeout=TIMEOUT)
                if len(rows) != len(part):
                    raise EmbedError(f"Ollama returned {len(rows)} vectors for {len(part)} texts.")
                for row in rows:
                    if dimension is None:
                        dimension = len(row)
                    if not row or len(row) != dimension:
                        raise EmbedError(f"Ollama returned vectors of {len(row)} values. The expected width is {dimension}.")
                    vectors.append(row)
    except ollama.OllamaError as exc:
        raise EmbedError(str(exc), exc.code) from exc
    except gateway.GatewayError as exc:
        raise _explain(exc, "Ollama") from exc
    return vectors


def endpoint_vectors(texts: list[str], *, model: str, dimension: int | None, base_url: str) -> list[list[float]]:
    """Vectors from an external OpenAI-compatible embeddings endpoint."""
    from app.backend.config import normalize_base_url
    from app.backend.pipeline import llm_client
    base = normalize_base_url(base_url or "")
    if not base or not model:
        raise EmbedError("The external embedding endpoint and model are not set. Set them under Retrieval in Settings.", "not_configured")
    client = llm_client.make(base, os.getenv("EMBED_API_KEY"), timeout=TIMEOUT, max_retries=2)
    host = base.split("//", 1)[-1].split("/", 1)[0]
    return _vectors(client, model, texts, dimension, truncate=False, where=f"The embedding endpoint at {host}")
