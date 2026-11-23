"""One body for every API answer that is an error.

Every error answer carries the same keys:

* ``ok``      always false
* ``error``   the short message, which the UI shows as the title
* ``detail``  the longer explanation (the same text when there is nothing more to say)
* ``hint``    what the user can do about it, or null
* ``code``    a stable machine-readable name (``not_found``, ``conflict``, ...)

Answers that are not errors keep their own bodies: a connection test that fails answers 200 with its result, a
sandbox without Docker answers 200 with ``docker_available: false``, and a view that is still being computed
answers 202 ``warming`` (see ``slowcache.py``).
"""
from __future__ import annotations

from fastapi.responses import JSONResponse

CODES = {
    400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed",
    409: "conflict", 413: "too_large", 422: "invalid_request", 500: "server_error", 502: "bad_gateway", 503: "unavailable",
}


def body(message: str, status: int = 400, *, detail: str | None = None, hint: str | None = None,
         code: str | None = None) -> dict:
    """The JSON body of an error answer."""
    message = str(message)
    return {"ok": False, "error": message, "detail": message if detail is None else str(detail),
            "hint": hint, "code": code or CODES.get(status, "error")}


def error(message: str, status: int = 400, *, detail: str | None = None, hint: str | None = None,
          code: str | None = None) -> JSONResponse:
    """An error answer with the unified body."""
    return JSONResponse(body(message, status, detail=detail, hint=hint, code=code), status_code=status)
