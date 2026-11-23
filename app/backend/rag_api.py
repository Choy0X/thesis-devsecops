"""Compatibility routes for the retired desktop embedding setup."""
from fastapi import APIRouter

from app.backend import errors

router = APIRouter()


@router.get("/api/rag/status")
def rag_status():
    return {"enabled": False, "retired": True, "backend": "local_search"}


@router.post("/api/rag/test-embedder")
def rag_test_embedder():
    return errors.error("Project search uses local search and has no embedding model to test.", 410, code="retired")
