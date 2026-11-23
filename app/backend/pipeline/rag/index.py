"""Build the embedded knowledge base from the train rows of a frozen split manifest.

The old Chroma index (``data/processed/chroma``, one document per severity-dataset row) is gone; see :mod:`kb` for what is indexed and why a
validation row can never be in it.
"""
from __future__ import annotations

from app.backend.pipeline.db import database as mongo
from app.backend.pipeline.rag import embed, kb


def latest_manifest_version(db) -> str | None:
    doc = db["split_manifests"].find_one(sort=[("_id", -1)])
    return doc["_id"] if doc else None


def main(kb_version: str | None = None, progress=None) -> dict:
    db = mongo.database()
    version = kb_version or latest_manifest_version(db)
    if not version:
        raise SystemExit("No split manifest exists yet, so there is no train/validation split to build a knowledge base from. "
                         "Build and freeze a dataset first.")
    embedder = embed.configured()
    print(f"embedding the train rows of {version} with {embedder.key}")
    out = kb.build_kb(db, embedder, version, progress=progress)
    print(f"knowledge base ready: {out['counts']} chunks in {out['collection']} ({out['embedded']} embedded, {out['unchanged']} unchanged, "
          f"{out['removed']} removed, {out['seconds']} s)")
    return out


if __name__ == "__main__":
    main()
